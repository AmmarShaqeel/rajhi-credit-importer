from setuptools import setup, find_packages

setup(
    name="rajhi_importer",
    version="0.1.0",
    description="An importer for Rajhi card PDF statements",
    author="Ammar Shaqeel",
    url="https://github.com/ammarshaqeew/rajhi-importer",
    packages=find_packages(),
    install_requires=[
        "beancount>=3.0.0",
        "pdfplumber>=0.7.0",
        "python-dateutil>=2.8.0",
        "beangulp>=0.2.0",
    ],
    python_requires=">=3.7",
    keywords="beancount, finance, accounting, pdf, import",
)
