"""
Aggregator module. Django expects `models.py` — we re-export
every model defined in the Models/ folder so migrations work.
"""
from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Profile_models import CustomerProfile
from myapp.Models.Banking_models import (
    PakistaniBank,
    ForeignBank,
    CustomerBankAccount,
    CustomerMerchantAccount,   # kept for legacy admin / historical data; not used by new flows
)
from myapp.Models.Transaction_models import (
    IncomingPayment,
    OutgoingPKRTransfer,
    TransactionStatusHistory,
)
from myapp.Models.Rate_models import ExchangeRate, ExchangeRateHistory
from myapp.Models.Fee_models import CustomerFeeConfig
from myapp.Models.Partner_models import Partner, PartnerShare, PartnerLedgerEntry
from myapp.Models.Report_models import DailyReport, WeeklyReport, MonthlyReport
from myapp.Models.Core_models import SystemSetting, Currency, PaymentMethod
from myapp.Models.Audit_models import AuditLog
from myapp.Models.Expense_models import Expense, ExpenseCategory
from myapp.Models.EmailOTP_models import EmailOTP
from myapp.Models.Invoicing_models import (
    Client, CustomerCompany, CustomerAllowedPaymentMethod,
    Invoice, InvoiceLineItem, InvoiceStatus, InvoicePaymentMethod,
)
from myapp.Models.Feature_models import CustomerFeatureAccess
from myapp.Models.InternalTx_models import (
    Vendor, USABankAccount, CreditCard, InternalPakistaniAccount,
    InternalTransaction,
    InternalTxSource, InternalTxDestination, InternalTxMethod,
)

__all__ = [
    "User", "UserRole",
    "CustomerProfile",
    "PakistaniBank", "ForeignBank",
    "CustomerBankAccount", "CustomerMerchantAccount",
    "IncomingPayment", "OutgoingPKRTransfer", "TransactionStatusHistory",
    "ExchangeRate", "ExchangeRateHistory",
    "CustomerFeeConfig",
    "Partner", "PartnerShare", "PartnerLedgerEntry",
    "DailyReport", "WeeklyReport", "MonthlyReport",
    "SystemSetting", "Currency", "PaymentMethod",
    "AuditLog",
    "Expense", "ExpenseCategory",
    "EmailOTP",
    "Client", "CustomerCompany", "CustomerAllowedPaymentMethod",
    "Invoice", "InvoiceLineItem", "InvoiceStatus", "InvoicePaymentMethod",
    "CustomerFeatureAccess",
    # Internal transactions module
    "Vendor", "USABankAccount", "CreditCard", "InternalPakistaniAccount",
    "InternalTransaction",
    "InternalTxSource", "InternalTxDestination", "InternalTxMethod",
]
