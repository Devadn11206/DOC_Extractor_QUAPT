"""
Production infrastructure adapters (Team Member 3).

Each subpackage implements an existing port or contract of the application against a real backend. Importing this package
imports no third-party client library: a driver is imported only when a connection is made, so an installation without the
matching optional extra still imports and runs everything else.

Currently: ``mongo`` (MongoDB foundation: configuration, connection, tenant-scoped repository base, index registry).
"""
