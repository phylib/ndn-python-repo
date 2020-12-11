from ndn.encoding import TlvModel, BytesField, UintField, NameField, ModelField, TypeNumber

TLV_ENCRYPTED_CONTENT_TYPE = 130
TLV_ENCRYPTED_PAYLOAD_TYPE = 132
TLV_IV_TYPE = 133
TLV_ENCRYPTED_PAYLOADKEY_TYPE = 134

# EncryptedContent from NAC
# EncryptedContent = ENCRYPTED-CONTENT-TYPE TLV-LENGTH
# EncryptedPayload
# [InitializationVector]
# [EncryptedPayloadKey]
# [Name]

# EncryptedPayload = ENCRYPTED-PAYLOAD-TYPE TLV-LENGTH *OCTET
# InitializationVector = INITIALIZATION-VECTOR-TYPE TLV-LENGTH *OCTET
# EncryptedPayloadKey = ENCRYPTED-PAYLOAD-KEY-TYPE TLV-LENGTH *OCTET

class EncryptedPack(TlvModel):
    payload = BytesField(TLV_ENCRYPTED_PAYLOAD_TYPE)
    iv = BytesField(TLV_IV_TYPE)
    payload_key = BytesField(TLV_ENCRYPTED_PAYLOADKEY_TYPE)
    name = NameField()

class EncryptedContent(TlvModel):
    pack = ModelField(TLV_ENCRYPTED_CONTENT_TYPE, EncryptedPack)