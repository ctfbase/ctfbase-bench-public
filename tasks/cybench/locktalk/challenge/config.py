from jwcrypto import jwk
import os

class Config:
    DEBUG = False
    FLAG = os.environ.get("FLAG", "FLAG{not_set}")
    JWT_SECRET_KEY = jwk.JWK.generate(kty='RSA', size=2048)