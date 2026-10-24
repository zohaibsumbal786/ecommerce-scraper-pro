from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# Use /data/shopify_scraper.db on Fly.io (persistent volume), fallback to local
DB_PATH = os.environ.get("DB_PATH", "./shopify_scraper.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _heal_stuck_scrapes()


def _heal_stuck_scrapes():
    """
    On startup, find any stores stuck in 'scraping' status
    (e.g. from a previous interrupted run) and mark them complete
    with accurate counts from the actual rows in the DB.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT id, domain FROM stores WHERE scrape_status = 'scraping'")
            )
            stuck = result.fetchall()
            if not stuck:
                return

            now = datetime.utcnow().isoformat()
            for store_id, domain in stuck:
                p_count = conn.execute(
                    text("SELECT COUNT(*) FROM products WHERE store_id = :sid"),
                    {"sid": store_id}
                ).scalar() or 0

                v_count = conn.execute(
                    text("""SELECT COUNT(*) FROM variants v
                            JOIN products p ON v.product_id = p.id
                            WHERE p.store_id = :sid"""),
                    {"sid": store_id}
                ).scalar() or 0

                i_count = conn.execute(
                    text("""SELECT COUNT(*) FROM images i
                            JOIN products p ON i.product_id = p.id
                            WHERE p.store_id = :sid"""),
                    {"sid": store_id}
                ).scalar() or 0

                conn.execute(
                    text("""UPDATE stores
                            SET scrape_status = 'complete',
                                total_products = :p,
                                total_variants = :v,
                                total_images   = :i,
                                scraped_at     = COALESCE(scraped_at, :now)
                            WHERE id = :sid"""),
                    {"p": p_count, "v": v_count, "i": i_count,
                     "now": now, "sid": store_id}
                )
                conn.commit()
                print(f"[DB heal] Fixed stuck store '{domain}': "
                      f"{p_count} products, {v_count} variants, {i_count} images → complete")
    except Exception as e:
        print(f"[DB heal] Warning: could not heal stuck scrapes: {e}")
