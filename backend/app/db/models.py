"""SQLite tables (SQLAlchemy).

Reference data: vendors, purchase_orders, po_lines.
Ledger:         approved_invoices. UNIQUE(vendor_id, invoice_number) is the database-level duplicate guard.
                Only an Approve decision ever inserts here.
Audit trail:    runs, stage_events, check_results, decisions. Written for EVERY run, whatever the outcome.
                (runs has no uniqueness on invoice number: re-running a file is allowed.)
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Money(TypeDecorator):
    """Decimal stored as TEXT, so SQLite never turns money into a float."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    gstin: Mapped[str] = mapped_column(String, unique=True)
    bank_account: Mapped[str] = mapped_column(String)
    ifsc: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    po_id: Mapped[str] = mapped_column(String, primary_key=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.vendor_id"))
    po_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)


class POLine(Base):
    __tablename__ = "po_lines"
    po_line_id: Mapped[str] = mapped_column(String, primary_key=True)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_id"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String)
    hsn: Mapped[str | None] = mapped_column(String)
    qty: Mapped[Decimal] = mapped_column(Money)
    unit: Mapped[str | None] = mapped_column(String)
    unit_price: Mapped[Decimal] = mapped_column(Money)


class ApprovedInvoice(Base):
    __tablename__ = "approved_invoices"
    __table_args__ = (UniqueConstraint("vendor_id", "invoice_number", name="uq_vendor_invoice"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.vendor_id"))
    invoice_number: Mapped[str] = mapped_column(String)
    po_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.po_id"))
    invoice_date: Mapped[date] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(Money)      # tax-exclusive
    tax_amount: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)         # incl. GST
    approved_on: Mapped[date] = mapped_column(Date)
    run_id: Mapped[int | None] = mapped_column(Integer)   # null for seeded history


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String, default="running")   # running | completed | failed
    doc_type: Mapped[str | None] = mapped_column(String)
    vendor_name: Mapped[str | None] = mapped_column(String)
    invoice_number: Mapped[str | None] = mapped_column(String)
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    total: Mapped[Decimal | None] = mapped_column(Money)
    decision: Mapped[str | None] = mapped_column(String)
    risk_level: Mapped[str | None] = mapped_column(String)
    top_reason: Mapped[str | None] = mapped_column(Text)


class StageEvent(Base):
    """One row per event: started / completed / failed / skipped, with payload and duration."""
    __tablename__ = "stage_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    stage: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    output: Mapped[dict | None] = mapped_column(JSON)     # masked
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CheckRow(Base):
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    stage: Mapped[str] = mapped_column(String)
    code: Mapped[str] = mapped_column(String)
    result: Mapped[str] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    risk: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON)          # masked


class DecisionRow(Base):
    __tablename__ = "decisions"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    decision: Mapped[str] = mapped_column(String)
    risk_level: Mapped[str] = mapped_column(String)
    top_reason: Mapped[str | None] = mapped_column(Text)
    routed_to: Mapped[str | None] = mapped_column(String)
    required_action: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    drafted_message: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
