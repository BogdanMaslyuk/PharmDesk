"""Inventory and medicines are separate; no medical assertions without sources."""
from datetime import date, datetime, timezone
from sqlalchemy import (
    Date, DateTime, ForeignKey, Integer, String, Text, Boolean,
    Table, Column, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

medicine_ingredients = Table(
    'medicine_ingredients', Base.metadata,
    Column('medicine_id', ForeignKey('medicines.id', ondelete='CASCADE'), primary_key=True),
    Column('ingredient_id', ForeignKey('ingredients.id', ondelete='CASCADE'), primary_key=True),
)


class Drawer(Base):
    __tablename__ = 'drawers'
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default='Без названия')
    location: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    stock_items: Mapped[list['StockItem']] = relationship(back_populates='drawer')


class Ingredient(Base):
    __tablename__ = 'ingredients'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    medicines: Mapped[list['Medicine']] = relationship(secondary=medicine_ingredients, back_populates='ingredients')


class Medicine(Base):
    __tablename__ = 'medicines'
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(350))
    manufacturer: Mapped[str | None] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(160))
    dosage: Mapped[str | None] = mapped_column(String(100))
    form: Mapped[str | None] = mapped_column(String(120))
    package: Mapped[str | None] = mapped_column(String(100))
    ingredients: Mapped[list[Ingredient]] = relationship(secondary=medicine_ingredients, back_populates='medicines')
    stock_items: Mapped[list['StockItem']] = relationship(back_populates='medicine')
    clinical_records: Mapped[list['ClinicalRecord']] = relationship(back_populates='medicine')


class StockItem(Base):
    __tablename__ = 'stock_items'
    __table_args__ = (UniqueConstraint('drawer_id', 'article_code', name='uq_drawer_article'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    article_code: Mapped[str] = mapped_column(String(80), index=True)
    drawer_id: Mapped[int] = mapped_column(ForeignKey('drawers.id'), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id'), index=True)
    added_on: Mapped[date | None] = mapped_column(Date)
    availability: Mapped[str] = mapped_column(String(32), default='не проверено')
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default='импорт')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    drawer: Mapped[Drawer] = relationship(back_populates='stock_items')
    medicine: Mapped[Medicine] = relationship(back_populates='stock_items')


class ClinicalRecord(Base):
    """Only approved sourced information should be published in the clinical UI."""
    __tablename__ = 'clinical_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id'))
    section: Mapped[str] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text)
    source_title: Mapped[str] = mapped_column(String(250))
    source_url: Mapped[str] = mapped_column(String(800))
    reviewed_on: Mapped[date] = mapped_column(Date)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    medicine: Mapped[Medicine] = relationship(back_populates='clinical_records')


class Interaction(Base):
    """Empty until a permitted, verified interactions dataset is connected."""
    __tablename__ = 'interactions'
    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_a_id: Mapped[int] = mapped_column(ForeignKey('ingredients.id'))
    ingredient_b_id: Mapped[int] = mapped_column(ForeignKey('ingredients.id'))
    severity: Mapped[str] = mapped_column(String(50))
    mechanism: Mapped[str] = mapped_column(Text)
    clinical_effect: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    source_title: Mapped[str] = mapped_column(String(250))
    source_url: Mapped[str] = mapped_column(String(800))
    reviewed_on: Mapped[date] = mapped_column(Date)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('ingredient_a_id', 'ingredient_b_id', name='uq_interaction_pair'),)

class InventoryEvent(Base):
    """Local audit trail for human edits, moves and imports; no patient data."""
    __tablename__ = 'inventory_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReviewRecord(Base):
    """Human-review queue imported from OCR; importing does NOT modify stock."""
    __tablename__ = "review_records"
    __table_args__ = (UniqueConstraint("drawer_number", "article_code", name="uq_review_drawer_code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    drawer_number: Mapped[int] = mapped_column(Integer, index=True)
    drawer_name: Mapped[str] = mapped_column(String(200), default="Без названия")
    article_code: Mapped[str] = mapped_column(String(80), index=True)
    proposed_name: Mapped[str] = mapped_column(String(350))
    proposed_manufacturer: Mapped[str] = mapped_column(String(300), default="")
    source_file: Mapped[str] = mapped_column(String(180))
    source_row: Mapped[int | None] = mapped_column(Integer)
    ocr_status: Mapped[str] = mapped_column(String(120), default="")
    quality_notes: Mapped[str] = mapped_column(Text, default="")
    original_name: Mapped[str] = mapped_column(String(350), default="")
    priority: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    confirmed_code: Mapped[str | None] = mapped_column(String(80))
    confirmed_name: Mapped[str | None] = mapped_column(String(350))
    confirmed_manufacturer: Mapped[str | None] = mapped_column(String(300))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReviewDecision(Base):
    """Immutable history of explicit human review decisions."""
    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("review_records.id"), index=True)
    action: Mapped[str] = mapped_column(String(24))
    previous_code: Mapped[str] = mapped_column(String(80))
    previous_name: Mapped[str] = mapped_column(String(350))
    selected_code: Mapped[str | None] = mapped_column(String(80))
    selected_name: Mapped[str | None] = mapped_column(String(350))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SourceImage(Base):
    """Local-only copy of a screenshot referenced by a review record."""
    __tablename__ = "source_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(180))
    name_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    stored_filename: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SubstanceEvidence(Base):
    """Manual drug-identity evidence; never automatically infer therapeutic claims."""
    __tablename__ = "substance_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"), index=True)
    substances_text: Mapped[str] = mapped_column(String(500))
    source_title: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(800))
    notes: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(String(24), default="manual")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
