"""
pipeline -- routes a product to the correct per-platform processing pipeline.
Callers (tasks.py) import this module rather than pipeline_s1/pipeline_s2
directly, so adding a third platform later (Sentinel-3, Sentinel-5P) is a
one-line addition here.
"""
from sqlalchemy.orm import Session

from vyom.models import CatalogProduct
from vyom.processing import pipeline_s1, pipeline_s2


def process_product(db: Session, product: CatalogProduct) -> CatalogProduct:
    if product.platform == "S1":
        return pipeline_s1.process_product(db, product)
    return pipeline_s2.process_product(db, product)
