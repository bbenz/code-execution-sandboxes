"""Keyless Azure credentials: managed identity in Azure, your azd or Azure CLI sign-in locally."""

from __future__ import annotations

import os


def sync_credential():
    from azure.identity import (
        AzureCliCredential,
        AzureDeveloperCliCredential,
        ChainedTokenCredential,
        ManagedIdentityCredential,
    )

    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    tenant_id = os.environ.get("AZURE_TENANT_ID") or None
    return ChainedTokenCredential(AzureDeveloperCliCredential(tenant_id=tenant_id),
                                  AzureCliCredential(tenant_id=tenant_id))


def async_credential():
    from azure.identity.aio import (
        AzureCliCredential,
        AzureDeveloperCliCredential,
        ChainedTokenCredential,
        ManagedIdentityCredential,
    )

    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    tenant_id = os.environ.get("AZURE_TENANT_ID") or None
    return ChainedTokenCredential(AzureDeveloperCliCredential(tenant_id=tenant_id),
                                  AzureCliCredential(tenant_id=tenant_id))
