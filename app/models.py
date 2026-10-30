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
