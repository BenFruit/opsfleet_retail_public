from .models import ColumnPolicy, DataAccessLevel

USER_COLUMN_POLICIES = {
    "id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "first_name": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "last_name": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "email": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "age": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "gender": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "state": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "street_address": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "postal_code": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "city": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "country": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "latitude": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "longitude": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
    "traffic_source": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "created_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "user_geom": ColumnPolicy(access_level=DataAccessLevel.BLOCKED_PII),
}


ORDERS_COLUMN_POLICIES = {
    "order_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "user_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "status": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "gender": ColumnPolicy(access_level=DataAccessLevel.AGGREGATE_ONLY),
    "created_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "returned_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "shipped_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "delivered_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "num_of_item": ColumnPolicy(
        access_level=DataAccessLevel.BLOCKED_UNSCOPED,
        description="Count authorized order_items instead of whole-order num_of_item",
    ),
}


ORDER_ITEMS_COLUMN_POLICIES = {
    "id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "order_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "user_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "product_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "inventory_item_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "status": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "created_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "shipped_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "delivered_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "returned_at": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "sale_price": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
}


PRODUCTS_COLUMN_POLICIES = {
    "id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "cost": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "category": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "name": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "brand": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "retail_price": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "department": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "sku": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
    "distribution_center_id": ColumnPolicy(access_level=DataAccessLevel.ALLOWED),
}


TABLE_POLICIES = {
    "users": USER_COLUMN_POLICIES,
    "orders": ORDERS_COLUMN_POLICIES,
    "order_items": ORDER_ITEMS_COLUMN_POLICIES,
    "products": PRODUCTS_COLUMN_POLICIES,
}

# Order is the order the model sees the tables in.
ANALYSIS_TABLES = ("orders", "order_items", "products", "users")
if set(ANALYSIS_TABLES) != set(TABLE_POLICIES):
    raise RuntimeError("Every analysis table needs a column policy")

CATALOG_NAME = "bigquery-public-data"
DB_NAME = "thelook_ecommerce"
