"""Personal Sign in with ChatGPT transport. Importing this package does no I/O."""

PLUGIN_NAME = "astrbot_plugin_sign_in_with_chatgpt"
PROVIDER_TYPE = "siwc_chat_completion"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
