# Terraform's Azure mock never provisions cloud resources. All test state is local.
mock_provider "azurerm" {
  mock_resource "azurerm_subscription_template_deployment" {
    defaults = {
      output_content = "{\"webAppUrl\":{\"value\":\"https://fixture.invalid\"},\"webAppName\":{\"value\":\"fixture\"},\"postgresHost\":{\"value\":\"fixture.invalid\"},\"storageAccount\":{\"value\":\"cloudlensstrv0913\"},\"managedIdentityName\":{\"value\":\"fixture-mi\"},\"managedIdentityPrincipalId\":{\"value\":\"44444444-4444-4444-8444-444444444444\"},\"managedIdentityClientId\":{\"value\":\"55555555-5555-4555-8555-555555555555\"}}"
    }
  }
}

variables {
  location                  = "eastus2"
  admin_principal_object_id = "11111111-1111-4111-8111-111111111111"
  admin_principal_name      = "fixture@example.invalid"
  storage_account_name      = "cloudlensstrv0913"
}

run "adopt_legacy_storage" {
  command = apply

  assert {
    condition     = jsondecode(azurerm_subscription_template_deployment.cloudlens.parameters_content).storageAccountName.value == "cloudlensstrv0913"
    error_message = "Migration must preserve the reviewed existing account."
  }

  assert {
    condition     = length(random_id.export_storage) == 0
    error_message = "Adopting existing storage must not generate a replacement name."
  }
}

run "redeploy_keeps_storage" {
  command = plan

  assert {
    condition     = terraform_data.export_storage_identity.output == "cloudlensstrv0913"
    error_message = "Redeployment changed the pinned account."
  }
}

run "reject_storage_rename" {
  command = plan

  variables {
    storage_account_name = "staccidentalreplacement"
  }

  expect_failures = [azurerm_subscription_template_deployment.cloudlens]
}
