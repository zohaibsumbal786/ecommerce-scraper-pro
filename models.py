from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    domain = Column(String, unique=True, index=True)
    original_url = Column(String)
    total_products = Column(Integer, default=0)
    total_variants = Column(Integer, default=0)
    total_images = Column(Integer, default=0)
    scrape_status = Column(String, default="pending")
    scraped_at = Column(DateTime, nullable=True)

    products = relationship("Product", back_populates="store", cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    shopify_id = Column(String, nullable=True)
    title = Column(String)
    handle = Column(String)
    product_type = Column(String, nullable=True)
    vendor = Column(String, nullable=True)
    tags = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    description_html = Column(Text, nullable=True)
    price_min = Column(Float, nullable=True)
    price_max = Column(Float, nullable=True)
    option1_name = Column(String, nullable=True)
    option2_name = Column(String, nullable=True)
    option3_name = Column(String, nullable=True)
    created_at_shopify = Column(String, nullable=True)
    updated_at_shopify = Column(String, nullable=True)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    store = relationship("Store", back_populates="products")
    variants = relationship("Variant", back_populates="product", cascade="all, delete-orphan")
    images = relationship("Image", back_populates="product", cascade="all, delete-orphan")


class Variant(Base):
    __tablename__ = "variants"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    shopify_variant_id = Column(String, nullable=True)
    title = Column(String, nullable=True)
    sku = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    compare_at_price = Column(Float, nullable=True)
    available = Column(Boolean, default=True)
    option1 = Column(String, nullable=True)
    option2 = Column(String, nullable=True)
    option3 = Column(String, nullable=True)

    product = relationship("Product", back_populates="variants")


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    position = Column(Integer, default=1)
    alt_text = Column(String, nullable=True)
    original_src = Column(String)
    local_path = Column(String, nullable=True)

    product = relationship("Product", back_populates="images")


class ShopifyCredential(Base):
    __tablename__ = "shopify_credentials"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)
    shop_domain = Column(String, nullable=False)
    access_token = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    publish_jobs = relationship("PublishJob", back_populates="credential", cascade="all, delete-orphan")


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id = Column(Integer, primary_key=True, index=True)
    credential_id = Column(Integer, ForeignKey("shopify_credentials.id"), nullable=False)
    source_store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    status = Column(String, default="pending")
    total_products = Column(Integer, default=0)
    published_products = Column(Integer, default=0)
    failed_products = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_log = Column(Text, nullable=True)

    credential = relationship("ShopifyCredential", back_populates="publish_jobs")
    source_store = relationship("Store")
