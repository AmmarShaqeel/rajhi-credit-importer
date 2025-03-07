"""
Importer for Rajhi Bank Credit Cards.
"""
__license__ = "GNU GPLv2"

import csv
import pdfplumber
from datetime import datetime
import re
import logging
import subprocess as sp
from decimal import ROUND_DOWN

from os import path
from io import StringIO
from dateutil.parser import parse
from collections import defaultdict
from dateutil.parser import parse as parse_datetime

from beancount.core import account
from beancount.core import amount
from beancount.core import data
from beancount.core import flags
from beancount.core import position
from beancount.core.number import D
from beancount.core.number import ZERO

import beangulp
from beangulp import mimetypes
from beangulp.cache import cache
from beangulp.testing import main


class Importer(beangulp.Importer):
    """An importer for Rajhi Bank PDF statements."""

    def __init__(self,account, currency, account_number, flag='*'):
        self.importer_account = account
        self.currency = currency
        self.flag = flag
        self.account_number = account_number

    def filename(self, filepath):
        # Normalize the name to something meaningful.
        return 'maybank.pdf'

    def account(self, filepath):
        return self.importer_account

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/pdf':
            return False

        # Look for some words in the PDF file to figure out if it's a statement
        # from Rajhi, by looking for the card number."
        with pdfplumber.open(filepath) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages])
        if text:
            return re.search(self.account_number, text) is not None

    def date(self, filepath):
        # Get the actual statement's date from the contents of the file.
        text = pdf_to_text(filepath)
        match = re.search('Date: ([^\n]*)', text)
        if match:
            return parse_datetime(match.group(1)).date()

    def extract(self,filepath, existing):
        """
        Process a single PDF file: extract text, parse transactions, 
        and save them as CSV and OFX files.
        """
        # open pdf and remove page dividers
        with pdfplumber.open(filepath) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages])
        default_account = self.account(filepath)
        currency = self.currency

        patterns = [
            r"ﺔﻴﻧﺎﻤﺘﺋﻻﺍ ﺔﻗﺎﻄﺒﻟﺍ ﺏﺎﺴﺣ ﻒﺸﻛ",
            r"credit card statement",
            r"Page no\. \d+ of \d+",
            r"ﺮﻬﺷ ﺏﺎﺴﺣ ﻒﺸﻛ",
            r"[A-Z]+, \d{4} Statement Month"
        ]
        
        clean_lines = []
        for line in text.splitlines():
            if not any(re.match(pattern, line) for pattern in patterns):
                clean_lines.append(line)
        
        text_lines = "\n".join(clean_lines)


        """Parse transactions from text lines according to specified rules"""
        lines = [line.strip() for line in text_lines.strip().split('\n') if line.strip()]

        entries  = []
        
        i = 0
        while i < len(lines):
            current_line = lines[i]
            
            # First, check if this is an Advance Payment line (Type A)
            advance_payment_pattern = r'^(CR\s+)?(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+Advance Payment\s+.+?\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+([A-Z]{3})\s+(\d{2}/\d{2}/\d{2})\s+(\d{2}/\d{2}/\d{2})$'
            advance_match = re.match(advance_payment_pattern, current_line)
            
            if advance_match or "Advance Payment" in current_line:
                # For Advance Payment, use a more general pattern if the specific one doesn't match
                if not advance_match:
                    general_pattern = r'^(CR\s+)?(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(.+?)\s+(\d{2}/\d{2}/\d{2})\s+(\d{2}/\d{2}/\d{2})$'
                    match = re.match(general_pattern, current_line)
                    
                    if match:
                        is_credit = bool(match.group(1))
                        amount = match.group(2)
                        fee = match.group(3)
                        remaining = match.group(4)
                        
                        # Try to extract net amount and currency
                        currency_pattern = r'(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+([A-Z]{3})'
                        currency_match = re.search(currency_pattern, remaining)
                        
                        if currency_match:
                            transaction_amount = currency_match.group(1)
                            transaction_currency = currency_match.group(2)
                        else:
                            transaction_amount = amount
                            currency_code_match = re.search(r'\b([A-Z]{3})\b', remaining)
                            transaction_currency = currency_code_match.group(1) if currency_code_match else None
                        
                        posting_date = match.group(5)
                        transaction_date = match.group(6)
                    else:
                        # Skip if we can't parse at all
                        i += 1
                        continue
                else:
                    is_credit = bool(advance_match.group(1))
                    amount = advance_match.group(2)
                    fee = advance_match.group(3)
                    transaction_amount = advance_match.group(4)
                    transaction_currency = advance_match.group(5)
                    posting_date = advance_match.group(6)
                    transaction_date = advance_match.group(7)
                    units = data.Amount(D(str(amount)) * (1 if is_credit else -1), currency)
                

                entry = data.Transaction(
                    data.new_metadata(filepath, 0, ()),
                    datetime.strptime(posting_date,'%d/%m/%y').date(), "*",
                    "Advance payment",
                    ("CR " if is_credit else "-")+ str(transaction_amount) + " " + transaction_currency + " on " + transaction_date,
                    data.EMPTY_SET,
                    data.EMPTY_SET,
                    [
                        data.Posting(default_account, units, None, None, None, None),
                    ],
                )
                entries.append(entry)
                i += 1
                continue
                
            # Now check if the line contains "Amount:" - Type B
            has_amount_keyword = "Amount:" in current_line
            
            # Standard pattern for all other transactions - with comma support
            pattern = r'^(CR\s+)?(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(.+?)\s+(\d{2}/\d{2}/\d{2})\s+(\d{2}/\d{2}/\d{2})$'
            
            # For lines with "Amount:" format - with comma support
            alt_pattern = r'^(CR\s+)?(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+Amount:\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+(.+?)\s+(\d{2}/\d{2}/\d{2})\s+(\d{2}/\d{2}/\d{2})$'
            
            match = re.match(pattern, current_line)
            alt_match = re.match(alt_pattern, current_line)
            
            if match or alt_match:
                # Determine transaction type based on "Amount:" presence
                transaction_type = "B" if has_amount_keyword else "C"
                
                if alt_match:
                    # Handle the "Amount:" format (Type B)
                    match = alt_match
                    is_credit = bool(match.group(1))
                    amount = match.group(2)
                    fee = match.group(3)
                    transaction_amount = match.group(4)
                    units = data.Amount(D(str(amount)) * (1 if is_credit else -1), currency)
                    
                    # Extract currency from the remaining part
                    remaining = match.group(5)
                    currency_pattern = r'(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+([A-Z]{3})'
                    currency_match = re.search(currency_pattern, remaining)
                    
                    transaction_currency = currency_match.group(2) if currency_match else None
                    
                    posting_date = match.group(6)
                    transaction_date = match.group(7)
                    
                    # Type B (has "Amount:") takes two lines above
                    payee = None
                    description = None
                    
                    if i >= 2:  # Need two lines above
                        payee = lines[i-2]
                        description = lines[i-1]
                
                else:
                    # Regular format (Type C)
                    is_credit = bool(match.group(1))
                    amount = match.group(2)
                    fee = match.group(3)
                    units = data.Amount(D(str(amount)) * (1 if is_credit else -1), currency)
                    
                    # The remaining part could have different formats
                    remaining = match.group(4)
                    
                    # Check if it has currency
                    currency_pattern = r'(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)\s+([A-Z]{3})'
                    currency_match = re.search(currency_pattern, remaining)
                    
                    if currency_match:
                        transaction_amount = currency_match.group(1)
                        transaction_currency = currency_match.group(2)
                    else:
                        # If no currency is found, the remaining part is directly the net amount
                        transaction_amount_match = re.search(r'(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)', remaining)
                        if transaction_amount_match:
                            transaction_amount = transaction_amount_match.group(0)
                        else:
                            transaction_amount = amount  # Default if not found
                        
                        # Check for currency code (3 uppercase letters)
                        currency_code_match = re.search(r'\b([A-Z]{3})\b', remaining)
                        transaction_currency = currency_code_match.group(1) if currency_code_match else None
                    
                    posting_date = match.group(5)
                    transaction_date = match.group(6)
                    
                    # NEW: Check if the next line has "Amount: X,XXX.XX" format
                    next_line_amount_pattern = r'^Amount:\s+(\d{1,3}(?:,\d{3})*\.\d+|\d+\.\d+)'
                    
                    if i + 1 < len(lines) and re.match(next_line_amount_pattern, lines[i+1]):
                        # If next line has "Amount:", treat it like type B (take two lines above)
                        payee = None
                        description = None
                        
                        if i >= 2:  # Need two lines above
                            payee = lines[i-2]
                            description = lines[i-1]
                        
                        # Skip the next line since we've already processed it as part of this transaction
                        i += 1
                    else:
                        # Original type C behavior (take line above and below)
                        payee = None
                        description = None
                        
                        if i > 0:  # Need line above
                            payee = lines[i-1]
                        if i + 1 < len(lines):  # Need line below
                            description = lines[i+1]
                
                # Create and append the transaction
                entry = data.Transaction(
                    data.new_metadata(filepath, 0, ()),
                    datetime.strptime(posting_date,'%d/%m/%y').date(), "*",
                    payee,
                    ("CR " if is_credit else "-")+ str(transaction_amount) + " " + transaction_currency + " on " + transaction_date,
                    data.EMPTY_SET,
                    data.EMPTY_SET,
                    [
                        data.Posting(default_account, units, None, None, None, None),
                    ],
                )
                entries.append(entry)
            
            i += 1
        
        return entries 



if __name__ == '__main__':
    importer = Importer("Liabilities:Rajhi:Bonvoy", "SAR")
    main(importer)
