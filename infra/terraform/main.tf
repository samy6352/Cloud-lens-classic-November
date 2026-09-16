# CloudLens for Terraform-managed environments.
#
# There is no second description of the stack here. This deploys the same Bicep,
# compiled to ARM, through Terraform -- so a customer whose platform team
# standardises on Terraform gets their state file, their pipeline and their
# `terraform plan`, while CloudLens keeps one source of truth in infra/main.bicep.
#
# Generate the template first:
#
#     az bicep build --file ../main.bicep --outfile ../main.json
#
# or run `../deploy.ps1 -EmitArm -WhatIf`.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  description = "Azure region for the stack."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create."
  type        = string
  default     = "rg-cloudlens"
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "cloudlens"
}

variable "storage_account_name" {
  description = "REQUIRED migration decision: set the actual existing storage account name on upgrade/import (from ARCHIVE_ACCOUNT_ID or deployment outputs). Set null explicitly ONLY for a genuinely new deployment, to generate a state-persisted random name. Never switch an existing deployment to null."
  type        = string
  nullable    = true

  validation {
    condition     = var.storage_account_name == null ? true : can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "Use the existing account's 3-24 lowercase letters/digits, or explicitly null for a new deployment."
  }
}

# No keepers: changing friendly names must not rotate the storage identity.
resource "random_id" "export_storage" {
  count       = var.storage_account_name == null ? 1 : 0
  byte_length = 10
}

locals {
  storage_account_name = var.storage_account_name == null ? "st${random_id.export_storage[0].hex}" : var.storage_account_name
}

# Pin reviewed legacy names too. Reject accidental edits rather than repointing the
# app/private endpoint while leaving the old data stranded in an incremental ARM deployment.
resource "terraform_data" "export_storage_identity" {
  input = local.storage_account_name

  lifecycle {
    ignore_changes = [input]
  }
}

variable "admin_principal_object_id" {
  description = "Object ID of the Entra principal that becomes the PostgreSQL administrator."
  type        = string
}

variable "admin_principal_name" {
  description = "Sign-in name of that administrator."
  type        = string
}

variable "admin_principal_type" {
  description = "User, Group or ServicePrincipal."
  type        = string
  default     = "User"
}

variable "network_isolation" {
  description = "True deploys PostgreSQL and storage with no public endpoints."
  type        = bool
  default     = true
}

variable "vnet_address_prefix" {
  description = "IPv4 CIDR for the new VNet when isolated; must not overlap connected networks. Preserve the deployed range on existing deployments."
  type        = string
  default     = "10.70.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vnet_address_prefix))
    error_message = "vnet_address_prefix must be a valid IPv4 CIDR."
  }
}

variable "app_subnet_address_prefix" {
  description = "IPv4 CIDR inside the VNet, disjoint from other subnets. Minimum size /28; /26 or larger recommended. Preserve the deployed range on existing deployments."
  type        = string
  default     = "10.70.1.0/24"

  validation {
    condition     = can(cidrnetmask(var.app_subnet_address_prefix))
    error_message = "app_subnet_address_prefix must be a valid IPv4 CIDR."
  }
}

variable "postgres_subnet_address_prefix" {
  description = "IPv4 CIDR inside the VNet, disjoint from other subnets. Minimum size /28. Preserve the deployed range on existing deployments."
  type        = string
  default     = "10.70.2.0/24"

  validation {
    condition     = can(cidrnetmask(var.postgres_subnet_address_prefix))
    error_message = "postgres_subnet_address_prefix must be a valid IPv4 CIDR."
  }
}

variable "private_endpoint_subnet_address_prefix" {
  description = "IPv4 CIDR inside the VNet, disjoint from other subnets, with sufficient usable addresses. Preserve the deployed range on existing deployments."
  type        = string
  default     = "10.70.3.0/24"

  validation {
    condition     = can(cidrnetmask(var.private_endpoint_subnet_address_prefix))
    error_message = "private_endpoint_subnet_address_prefix must be a valid IPv4 CIDR."
  }
}

# The subscription-scope template creates its own resource group, so the
# deployment itself is scoped to the subscription rather than to a group.
resource "azurerm_subscription_template_deployment" "cloudlens" {
  name             = "cloudlens"
  location         = var.location
  template_content = file("${path.module}/../main.json")

  parameters_content = jsonencode({
    location                           = { value = var.location }
    resourceGroupName                  = { value = var.resource_group_name }
    namePrefix                         = { value = var.name_prefix }
    storageAccountName                 = { value = terraform_data.export_storage_identity.output }
    adminPrincipalObjectId             = { value = var.admin_principal_object_id }
    adminPrincipalName                 = { value = var.admin_principal_name }
    adminPrincipalType                 = { value = var.admin_principal_type }
    networkIsolation                   = { value = var.network_isolation }
    vnetAddressPrefix                  = { value = var.vnet_address_prefix }
    appSubnetAddressPrefix             = { value = var.app_subnet_address_prefix }
    postgresSubnetAddressPrefix        = { value = var.postgres_subnet_address_prefix }
    privateEndpointSubnetAddressPrefix = { value = var.private_endpoint_subnet_address_prefix }
    developerClientIp                  = { value = "" }
  })

  lifecycle {
    precondition {
      condition     = local.storage_account_name == terraform_data.export_storage_identity.output
      error_message = "Export storage identity changed. Restore the original storage_account_name and Terraform state; storage must not be renamed on redeployment."
    }
  }
}

locals {
  outputs = jsondecode(azurerm_subscription_template_deployment.cloudlens.output_content)
}

output "web_app_url" {
  value = local.outputs.webAppUrl.value
}

output "web_app_name" {
  value = local.outputs.webAppName.value
}

output "postgres_host" {
  value = local.outputs.postgresHost.value
}

output "storage_account" {
  value = local.outputs.storageAccount.value
}

# deploy.ps1 grants this principal Cost Management Reader on the subscriptions
# being analysed. Under Terraform that grant belongs in the customer's own
# configuration, where the list of in-scope subscriptions lives.
output "managed_identity_principal_id" {
  description = "Object ID of the runtime managed identity. Assign cost roles to this ID, not the deployer or the Entra sign-in client ID."
  value       = local.outputs.managedIdentityPrincipalId.value
}

output "cost_access_handoff" {
  description = "Non-secret cost-access instructions. This output does not grant or verify subscription roles."
  value = {
    identity_name                   = local.outputs.managedIdentityName.value
    principal_object_id             = local.outputs.managedIdentityPrincipalId.value
    client_id                       = local.outputs.managedIdentityClientId.value
    assignment_scope                = "/subscriptions/<cost-subscription-id>"
    required_cost_roles             = ["Reader", "Cost Management Reader"]
    optional_export_management_role = "Cost Management Contributor"
    grants_verified                 = false
    instructions                    = "Use infra/grant-cost-access.ps1 for the existing web app and selected cost subscriptions. The operator needs role-assignment rights; do not grant subscription Owner to the application."
  }
}
