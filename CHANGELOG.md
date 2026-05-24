# CHANGELOG

<!-- version list -->

## v1.8.0 (2026-05-24)

### Chores

- Update comment-commander image
  ([`cfe27de`](https://github.com/MagmaMoose/comment-commander/commit/cfe27debe618abec0da7e757fcb0b11a9b30ca16))

### Features

- **processor**: Post-webhook sweep so a single trigger handles every comment
  ([`813019c`](https://github.com/MagmaMoose/comment-commander/commit/813019c2a3c4cc04f49a9ec35ddd1c96fcfadc13))


## v1.7.0 (2026-05-22)

### Chores

- Update comment-commander image
  ([`e97e9da`](https://github.com/MagmaMoose/comment-commander/commit/e97e9daa214b00e1f881d96ec1fdbd86b47034e7))

### Features

- **process**: Track webhook runs + Slack permalinks; fix manual-rerun comment cap
  ([`3d97038`](https://github.com/MagmaMoose/comment-commander/commit/3d970380878df401ee903589e636163463c5f7d7))

### Performance Improvements

- **processor**: Short-circuit pending loop to cap thread lookups
  ([`3618b37`](https://github.com/MagmaMoose/comment-commander/commit/3618b37b5496fe5d468b4eff799ea7a672602306))


## v1.6.1 (2026-05-22)

### Bug Fixes

- **processor**: Serialize PR processing to stop concurrent push races
  ([`43bdefe`](https://github.com/MagmaMoose/comment-commander/commit/43bdefe58fae0a9785dd4c3329787c51a5eca34c))

### Testing

- Synchronize concurrency test with Barrier for deterministic failure
  ([`e3eb88e`](https://github.com/MagmaMoose/comment-commander/commit/e3eb88ec90fe5d17b47bb597ca844e97b8464dc6))


## v1.6.0 (2026-05-22)

### Bug Fixes

- Report error status when process_pr_manual fails early
  ([`d7cfbc2`](https://github.com/MagmaMoose/comment-commander/commit/d7cfbc228ed7ec0e430cca70d375e7ac46994001))

- **processor**: Record decision only after successful reply/resolve
  ([`f6a8793`](https://github.com/MagmaMoose/comment-commander/commit/f6a87932b729fbdaf1ea43932a871823fcd2407e))

### Chores

- Bump image tag to v1.5.1
  ([`28c4369`](https://github.com/MagmaMoose/comment-commander/commit/28c43690e219c6457fd9748933cfde249b9a02c4))

### Features

- **process**: Accept GHE PR URLs + expose trigger results for polling
  ([`602930a`](https://github.com/MagmaMoose/comment-commander/commit/602930aef7dfd25df125ee839daa34898941b8ee))


## v1.5.1 (2026-05-21)

### Bug Fixes

- **setup-webhook**: Idempotent + propagate upstream 4xx status
  ([`4b57b39`](https://github.com/MagmaMoose/comment-commander/commit/4b57b39dfe0d520882c5401ba86a0e7ce7a424d3))

### Chores

- Bump image tag to v1.5.0
  ([`3b96758`](https://github.com/MagmaMoose/comment-commander/commit/3b967584d69d392da97e5ea7466bcdb865df15f1))


## v1.5.0 (2026-05-21)

### Chores

- Bump image tag to v1.4.0
  ([`c374472`](https://github.com/MagmaMoose/comment-commander/commit/c374472ad91edb867955cd6011e45590ece08b5d))

### Features

- Multi-instance (GHE), INVOLVED_USERS whitelist, POST /setup-webhook
  ([`f45c1ec`](https://github.com/MagmaMoose/comment-commander/commit/f45c1ecf875c19f4a0f7bbf72100c66a4e939c5c))


## v1.4.0 (2026-05-21)

### Chores

- Bump image tag to v1.3.0
  ([`a8e2349`](https://github.com/MagmaMoose/comment-commander/commit/a8e2349e0bb3bf521c80d19175f500bf29b2dda3))

### Features

- **api**: POST /process to manually re-walk every comment on a PR
  ([`661da8b`](https://github.com/MagmaMoose/comment-commander/commit/661da8b627f73374efc2962fadeb58c020f176b4))


## v1.3.0 (2026-05-21)

### Chores

- Bump image tag to v1.2.0
  ([`780f041`](https://github.com/MagmaMoose/comment-commander/commit/780f04134bf2f30d5a99246e602c6e7a3be2466a))

### Features

- **slack**: Post per-comment decision notifications to Slack
  ([`862cce8`](https://github.com/MagmaMoose/comment-commander/commit/862cce80bcdad7fc331002f01a0cb423ead576a3))


## v1.2.0 (2026-05-21)

### Chores

- Bump image tag to v1.1.4
  ([`2b1522c`](https://github.com/MagmaMoose/comment-commander/commit/2b1522ca18f8952b20b11012d32cdf32817807b5))

### Features

- **commits**: Enforce Conventional Commits 1.0.0
  ([`a8c6a43`](https://github.com/MagmaMoose/comment-commander/commit/a8c6a43ff90f744c3edc7146457f97b87af3cc1e))

- **observability**: Structured event logs at every stage, quieter health probes
  ([`a8c6a43`](https://github.com/MagmaMoose/comment-commander/commit/a8c6a43ff90f744c3edc7146457f97b87af3cc1e))


## v1.1.4 (2026-05-21)

### Bug Fixes

- **signing**: Write public-key file so ssh-keygen can sign commits
  ([`9b11061`](https://github.com/MagmaMoose/comment-commander/commit/9b110610d0f7a66abbc4f16c866e690885b2e986))

### Chores

- Bump image tag to v1.1.3
  ([`27f220b`](https://github.com/MagmaMoose/comment-commander/commit/27f220b98fe292ec15218e1533000f9db0ab4214))


## v1.1.3 (2026-05-21)

### Bug Fixes

- **observability**: Log stderr/stdout when git subprocesses fail
  ([`e1ac9b8`](https://github.com/MagmaMoose/comment-commander/commit/e1ac9b8c94cdfabf1cfc5f40d41a566dac24da46))

### Chores

- Bump image tag to v1.1.2
  ([`8016423`](https://github.com/MagmaMoose/comment-commander/commit/8016423d8130f29119fa79cc3492ca1a30164d07))


## v1.1.2 (2026-05-21)

### Bug Fixes

- **observability**: Log full traceback when background webhook task fails
  ([`8d81722`](https://github.com/MagmaMoose/comment-commander/commit/8d817223a23702ccba764e0e7070d38a228e114c))

### Chores

- Bump image tag to v1.1.1
  ([`aee0275`](https://github.com/MagmaMoose/comment-commander/commit/aee0275c9686c3df4ba8cbf1c2c2d4c992b055ca))


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
