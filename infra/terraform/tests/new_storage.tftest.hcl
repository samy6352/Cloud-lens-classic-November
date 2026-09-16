# Azure is mocked; random_id uses only local cryptographic randomness and Terraform state.
mock_provider "azurerm" {
  mock_resource "azurerm_subscription_template_deployment" {
    defaults = {
      output_content = "{\"webAppUrl\":{\"value\":\"https://fixture.invalid\"},\"webAppName\":{\"value\":\"fixture\"},\"postgresHost\":{\"value\":\"fixture.invalid\"},\"storageAccount\":{\"value\":\"fixture\"},\"managedIdentityName\":{\"value\":\"fixture-mi\"},\"managedIdentityPrincipalId\":{\"value\":\"44444444-4444-4444-8444-444444444444\"},\"managedIdentityClientId\":{\"value\":\"55555555-5555-4555-8555-555555555555\"}}"
    }
  }
}

variables {
  location                  = "eastus2"
  admin_principal_object_id = "11111111-1111-4111-8111-111111111111"
  admin_principal_name      = "fixture@example.invalid"
  storage_account_name      = null
}

run "generate_storage_once" {
  command = apply

  assert {
    condition     = can(regex("^st[0-9a-f]{20}$", terraform_data.export_storage_identity.output))
    error_message = "New storage must have an 80-bit random token, not friendly-name-derived storage."
  }

  assert {
    condition     = jsondecode(azurerm_subscription_template_deployment.cloudlens.parameters_content).storageAccountName.value == "st${random_id.export_storage[0].hex}"
    error_message = "The template must receive the state-persisted random name."
  }

  assert {
    condition     = output.cost_access_handoff.principal_object_id == "44444444-4444-4444-8444-444444444444" && output.cost_access_handoff.principal_object_id != var.admin_principal_object_id && output.cost_access_handoff.principal_object_id != output.cost_access_handoff.client_id
    error_message = "The grant recipient must be the runtime MI object ID, not the operator or client ID."
  }

  assert {
    condition     = length(output.cost_access_handoff.required_cost_roles) == 2 && contains(output.cost_access_handoff.required_cost_roles, "Reader") && contains(output.cost_access_handoff.required_cost_roles, "Cost Management Reader") && output.cost_access_handoff.optional_export_management_role == "Cost Management Contributor" && !output.cost_access_handoff.grants_verified
    error_message = "Cost access must distinguish required reads, optional export writes and unverified grants."
  }
}

run "friendly_names_do_not_rotate_storage" {
  command = plan

  variables {
    name_prefix = "renamed"
  }

  assert {
    condition     = terraform_data.export_storage_identity.output == "st${random_id.export_storage[0].hex}"
    error_message = "Changing a friendly name must not regenerate export storage."
  }
}
