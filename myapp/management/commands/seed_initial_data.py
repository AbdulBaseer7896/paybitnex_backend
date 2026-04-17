"""
Seed initial reference data.

Usage:
    python manage.py seed_initial_data
    python manage.py seed_initial_data --force   # overwrite existing entries

Seeds:
    - Currencies (PKR base + USD/EUR/GBP)
    - Pakistani banks (major list)
    - Foreign banks (USA/UK/EU)
    - Default system settings
"""
from django.core.management.base import BaseCommand

from myapp.Models.Banking_models import PakistaniBank, ForeignBank
from myapp.Models.Core_models import Currency, SystemSetting


PK_BANKS = [
    ("Allied Bank Limited", "ABL"),
    ("Askari Bank", "AKBL"),
    ("Bank Alfalah", "BAFL"),
    ("Bank Al Habib", "BAHL"),
    ("BankIslami Pakistan", "BIPL"),
    ("Dubai Islamic Bank Pakistan", "DIBPL"),
    ("Faysal Bank", "FABL"),
    ("Habib Bank Limited", "HBL"),
    ("Habib Metropolitan Bank", "HMB"),
    ("JS Bank", "JSBL"),
    ("MCB Bank", "MCB"),
    ("MCB Islamic Bank", "MCBIB"),
    ("Meezan Bank", "MEBL"),
    ("National Bank of Pakistan", "NBP"),
    ("Samba Bank", "SMBL"),
    ("Silkbank Limited", "SILK"),
    ("Soneri Bank", "SNBL"),
    ("Standard Chartered Bank Pakistan", "SCB"),
    ("Summit Bank", "SMBL2"),
    ("The Bank of Khyber", "BOK"),
    ("The Bank of Punjab", "BOP"),
    ("United Bank Limited", "UBL"),
    ("Zarai Taraqiati Bank Limited", "ZTBL"),
    ("EasyPaisa (Telenor Microfinance Bank)", "EP"),
    ("JazzCash (Mobilink Microfinance Bank)", "JC"),
    ("SadaPay", "SADA"),
    ("NayaPay", "NAYA"),
    ("Raast (SBP Instant Payment)", "RAAST"),
]

# country, bank name
FOREIGN_BANKS = [
    ("USA", "Bank of America"),
    ("USA", "Chase (JPMorgan Chase)"),
    ("USA", "Wells Fargo"),
    ("USA", "Citibank"),
    ("USA", "Capital One"),
    ("USA", "US Bank"),
    ("USA", "PNC Bank"),
    ("USA", "TD Bank"),
    ("USA", "HSBC USA"),
    ("USA", "Truist"),
    ("USA", "Payoneer"),
    ("USA", "Wise (TransferWise)"),
    ("USA", "Mercury"),
    ("USA", "Brex"),
    ("USA", "Revolut USA"),
    ("UK", "Barclays"),
    ("UK", "HSBC UK"),
    ("UK", "Lloyds Bank"),
    ("UK", "NatWest"),
    ("UK", "Santander UK"),
    ("UK", "Standard Chartered UK"),
    ("UK", "Monzo"),
    ("UK", "Starling Bank"),
    ("UK", "Revolut UK"),
    ("UK", "Wise UK"),
    ("EU", "Deutsche Bank"),
    ("EU", "BNP Paribas"),
    ("EU", "ING"),
    ("EU", "Santander"),
    ("EU", "UniCredit"),
    ("EU", "Commerzbank"),
    ("EU", "N26"),
    ("EU", "Revolut EU"),
    ("EU", "Wise EU"),
]

CURRENCIES = [
    # code, name, symbol, is_base, sort_order
    ("PKR", "Pakistani Rupee", "₨", True,  0),
    ("USD", "US Dollar",       "$", False, 1),
    ("EUR", "Euro",            "€", False, 2),
    ("GBP", "British Pound",   "£", False, 3),
]

DEFAULT_SETTINGS = [
    ("default_fee_percentage", "5.00",
     "Default transaction fee percentage when no customer override exists."),
    ("min_transaction_amount", "10",
     "Minimum foreign-currency amount per transaction."),
    ("max_transaction_amount", "100000",
     "Maximum foreign-currency amount per transaction."),
    ("require_email_screenshot", "false",
     "If true, customers MUST attach an email screenshot."),
    ("rate_buffer_percentage", "0.00",
     "Spread applied on top of live rates (e.g. 1.5 = -1.5%)."),
    ("auto_approve_kyc", "false",
     "Skip manual KYC review (NOT recommended)."),
    ("company_name", "PayBitnex",
     "Company display name."),
    ("support_email", "support@paybitnex.com",
     "Support contact email."),
]


class Command(BaseCommand):
    help = "Seed currencies, banks, and default system settings."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Overwrite existing rows.")

    def handle(self, *args, **opts):
        force = opts["force"]

        # Currencies
        for code, name, symbol, is_base, order in CURRENCIES:
            if force:
                Currency.objects.update_or_create(
                    code=code,
                    defaults=dict(name=name, symbol=symbol,
                                  is_base=is_base, sort_order=order, is_active=True),
                )
            else:
                Currency.objects.get_or_create(
                    code=code,
                    defaults=dict(name=name, symbol=symbol,
                                  is_base=is_base, sort_order=order, is_active=True),
                )
        self.stdout.write(self.style.SUCCESS(f"Currencies: {Currency.objects.count()}"))

        # Pakistani banks
        for name, code in PK_BANKS:
            if force:
                PakistaniBank.objects.update_or_create(
                    name=name, defaults=dict(short_code=code, is_active=True),
                )
            else:
                PakistaniBank.objects.get_or_create(
                    name=name, defaults=dict(short_code=code, is_active=True),
                )
        self.stdout.write(self.style.SUCCESS(
            f"Pakistani banks: {PakistaniBank.objects.count()}"
        ))

        # Foreign banks
        for country, name in FOREIGN_BANKS:
            if force:
                ForeignBank.objects.update_or_create(
                    name=name, country=country, defaults=dict(is_active=True),
                )
            else:
                ForeignBank.objects.get_or_create(
                    name=name, country=country, defaults=dict(is_active=True),
                )
        self.stdout.write(self.style.SUCCESS(
            f"Foreign banks: {ForeignBank.objects.count()}"
        ))

        # System settings
        for key, value, desc in DEFAULT_SETTINGS:
            if force:
                SystemSetting.objects.update_or_create(
                    key=key, defaults=dict(value=value, description=desc),
                )
            else:
                SystemSetting.objects.get_or_create(
                    key=key, defaults=dict(value=value, description=desc),
                )
        self.stdout.write(self.style.SUCCESS(
            f"System settings: {SystemSetting.objects.count()}"
        ))

        self.stdout.write(self.style.SUCCESS("Seed complete."))
