export function isPasskeySupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    Boolean(window.isSecureContext) &&
    typeof window.PublicKeyCredential !== 'undefined' &&
    Boolean(navigator.credentials)
  );
}

function base64UrlToUint8Array(base64url: string): Uint8Array<ArrayBuffer> {
  const pad = '='.repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const buf = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buf);
  for (let i = 0; i < raw.length; i++) {
    out[i] = raw.charCodeAt(i);
  }
  return out;
}

function bufferToBase64Url(buf: ArrayBuffer | null | undefined): string {
  if (!buf) return '';
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) {
    bin += String.fromCharCode(bytes[i]);
  }
  return window.btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function prepareCreationOptions(options: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const next = { ...options } as unknown as PublicKeyCredentialCreationOptions & {
    challenge: string | BufferSource;
    user: { id: string | BufferSource; name: string; displayName: string };
    excludeCredentials?: Array<{ id: string | BufferSource; type: PublicKeyCredentialType; transports?: AuthenticatorTransport[] }>;
  };

  next.challenge = base64UrlToUint8Array(String(options.challenge));
  const userObj = (options.user || {}) as Record<string, unknown>;
  next.user = {
    ...(userObj as unknown as PublicKeyCredentialUserEntity),
    id: base64UrlToUint8Array(String(userObj.id || '')),
  };

  if (Array.isArray(options.excludeCredentials)) {
    next.excludeCredentials = options.excludeCredentials.map((cred) => {
      const rec = cred as Record<string, unknown>;
      return {
        ...(rec as unknown as PublicKeyCredentialDescriptor),
        id: base64UrlToUint8Array(String(rec.id || '')),
      };
    });
  }
  return next as PublicKeyCredentialCreationOptions;
}

export function prepareRequestOptions(options: Record<string, unknown>): PublicKeyCredentialRequestOptions {
  const next = { ...options } as unknown as PublicKeyCredentialRequestOptions & {
    challenge: string | BufferSource;
    allowCredentials?: Array<{ id: string | BufferSource; type: PublicKeyCredentialType; transports?: AuthenticatorTransport[] }>;
  };
  next.challenge = base64UrlToUint8Array(String(options.challenge));
  if (Array.isArray(options.allowCredentials)) {
    next.allowCredentials = options.allowCredentials.map((cred) => {
      const rec = cred as Record<string, unknown>;
      return {
        ...(rec as unknown as PublicKeyCredentialDescriptor),
        id: base64UrlToUint8Array(String(rec.id || '')),
      };
    });
  }
  return next as PublicKeyCredentialRequestOptions;
}

export function serializeRegistrationCredential(cred: PublicKeyCredential): Record<string, unknown> {
  const resp = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: bufferToBase64Url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment ?? null,
    clientExtensionResults: cred.getClientExtensionResults(),
    transports: typeof resp.getTransports === 'function' ? resp.getTransports() : [],
    response: {
      clientDataJSON: bufferToBase64Url(resp.clientDataJSON),
      attestationObject: bufferToBase64Url(resp.attestationObject),
      transports: typeof resp.getTransports === 'function' ? resp.getTransports() : [],
    },
  };
}

export function serializeAuthenticationCredential(cred: PublicKeyCredential): Record<string, unknown> {
  const resp = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufferToBase64Url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment ?? null,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bufferToBase64Url(resp.clientDataJSON),
      authenticatorData: bufferToBase64Url(resp.authenticatorData),
      signature: bufferToBase64Url(resp.signature),
      userHandle: resp.userHandle ? bufferToBase64Url(resp.userHandle) : null,
    },
  };
}

export function normalizeWebAuthnError(err: unknown): string {
  if (!(err instanceof Error)) return 'Passkey operation failed.';
  if (err.name === 'NotAllowedError' || err.name === 'AbortError') {
    return 'Passkey prompt was cancelled or timed out.';
  }
  if (err.name === 'InvalidStateError') {
    return 'This passkey is already registered on this device.';
  }
  if (err.name === 'SecurityError') {
    return 'Passkeys require HTTPS or localhost.';
  }
  return err.message || 'Passkey operation failed.';
}

