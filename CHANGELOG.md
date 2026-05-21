# CHANGELOG

<!-- version list -->

## v1.1.1 (2026-05-21)

### Bug Fixes

- **bot**: Match Copilot's Code Review identity (login `Copilot`) and lower-case logins
  ([`e014d9a`](https://github.com/MagmaMoose/comment-commander/commit/e014d9a8b7f9c4ff27739dc0659805e2695a352f))


## v1.1.0 (2026-05-21)

### Bug Fixes

- **k8s**: Use the operator's actual Secret key name (private-key)
  ([`1429eaa`](https://github.com/MagmaMoose/comment-commander/commit/1429eaaa2b64f73e0ad0631adc1d7d38a7c7dca1))

### Chores

- Bump image tag to v1.0.3
  ([`dc3e91e`](https://github.com/MagmaMoose/comment-commander/commit/dc3e91ecec7167481af7d2397353ebff6bae0a20))

### Features

- **k8s**: Add DNS-only Ingress so external-dns publishes the CNAME
  ([`ef3c6bc`](https://github.com/MagmaMoose/comment-commander/commit/ef3c6bce9d5f0320ae5fb4b0bee4e69ea84864fe))


## v1.0.3 (2026-05-21)

### Bug Fixes

- **k8s**: Point OnePasswordItem at the new Tech vault
  ([`b0dda25`](https://github.com/MagmaMoose/comment-commander/commit/b0dda257aa2785ad1a31ea0be496ea3129a7c844))

### Chores

- Bump image tag to v1.0.1
  ([`09938ac`](https://github.com/MagmaMoose/comment-commander/commit/09938ac49729eb44c9cf681774d2134b2535f26a))


## v1.0.2 (2026-05-21)

### Bug Fixes

- **k8s**: Add OCI-Vault-backed ghcr-pull-secret for the private image
  ([`fa20554`](https://github.com/MagmaMoose/comment-commander/commit/fa205545fc6fbdd8cf8b06ee62d166e8a44ab2cb))


## v1.0.1 (2026-05-21)

### Bug Fixes

- **k8s**: Use a valid Secret key for the SSH signing private key
  ([`ffe8779`](https://github.com/MagmaMoose/comment-commander/commit/ffe87798dfc0015ea923a1bfa8acc4ada1516f86))


## v1.0.0 (2026-05-21)

- Initial Release
