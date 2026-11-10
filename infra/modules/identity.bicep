// The Entra app registration people sign in through.
//
// Optional, and deliberately separate from the stack. An app registration is a
// tenant-level object, and in most organisations the identity team owns those --
// a subscription deployment is not allowed to create one, and should not assume
// it can. So the default is that this already exists and its client ID is passed
// in; this module is for the case where the deployer does hold the directory
// rights and wants one command instead of two.
//
// Deployed with the Microsoft Graph Bicep extension, GA since July 2025. Before
// that, ARM could not create Entra objects at all and the only options were a
// deploymentScript running a container, or Terraform's azuread provider -- which
// was the strongest remaining argument for Terraform here, and no longer is.
//
// No client secret is created, because the extension cannot create one and
// CloudLens does not want one: it signs users in as a public client using PKCE.
// That is the better shape anyway. A secret in a customer deployment is a thing
// that expires at an unhelpful moment and has to be rotated by someone who has
// forgotten it exists.
//
// Which is why the redirect URIs go under `publicClient` and not `web`. A `web`
// redirect URI makes this a *confidential* client, and Entra then refuses to
// redeem the authorization code without a secret (AADSTS7000218) however correct
// everything else is. Tenants that cap or forbid secrets -- and the ones running
// a cost tool usually do -- leave a deployment that looks finished and turns
// everyone away at the door.
extension microsoftGraphV1_0

@description('Display name for the app registration.')
param displayName string = 'CloudLens'

@description('Stable unique name. Changing it creates a second registration rather than renaming this one.')
param uniqueName string = 'cloudlens'

@description('Redirect URIs. Include https://<web-app>/auth/callback, and a localhost URI for development.')
param redirectUris array

param delegatedArm bool = false

// Microsoft Graph, and the two delegated scopes needed to sign someone in and
// learn their name. Nothing more: CloudLens reads cost data with its own managed
// identity, not with the user's Graph token.
var graphAppId = '00000003-0000-0000-c000-000000000000'
var userReadScope = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
var openidScope = '37f7f235-527c-4136-accd-4a02d197296e'
var profileScope = '14dad69e-099b-42c9-810b-d002981feec1'
var offlineAccessScope = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'

// Azure Resource Manager, so the app can ask ARM questions as the signed-in
// person rather than as itself.
//
// This is what makes the subscription list correct. Asking ARM as the app is
// bounded by what the app's own identity can read, so someone with access to
// thirty subscriptions sees only the ones the app was also granted Reader on --
// and that undercount is silent. For a cost tool, quietly missing money is the
// worst failure available, so the app asks with the user's own token and lets
// Azure decide.
var armAppId = '797f4846-ba00-4fd7-ba43-dac1f8f63013'
var armUserImpersonation = '41094075-9dad-400e-a0bd-54e686782033'

resource application 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: displayName
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: true
  publicClient: {
    redirectUris: redirectUris
  }
  web: {
    implicitGrantSettings: {
      // The authorization code flow with PKCE returns nothing through the
      // browser URL, so neither implicit grant is needed. Leaving them on is how
      // a token ends up in browser history and server logs.
      enableAccessTokenIssuance: false
      enableIdTokenIssuance: false
    }
  }
  requiredResourceAccess: concat([
    {
      resourceAppId: graphAppId
      resourceAccess: [
        { id: openidScope, type: 'Scope' }
        { id: profileScope, type: 'Scope' }
        { id: offlineAccessScope, type: 'Scope' }
        { id: userReadScope, type: 'Scope' }
      ]
    }
  ], delegatedArm ? [
    {
      resourceAppId: armAppId
      resourceAccess: [
        { id: armUserImpersonation, type: 'Scope' }
      ]
    }
  ] : [])
}

resource servicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: application.appId
}

output clientId string = application.appId
output objectId string = application.id
output servicePrincipalId string = servicePrincipal.id
