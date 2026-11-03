"""Policy for model-written SQL: validation, demographic shapes and brand scoping.

validator.py decides whether a query may run; demographics.py checks the
restricted shapes allowed over aggregate-only columns; scope.py rewrites a
validated query so every source is limited to the trusted brands.
"""
