## [0.1.0-dev.4](https://github.com/IMXEren/Suwayomi-Server-docker/compare/v0.1.0-dev.3...v0.1.0-dev.4) (2026-09-20)

### Bug Fixes

* **deploy:** keep the served WebUI in sync with the bundled one ([66aca1b](https://github.com/IMXEren/Suwayomi-Server-docker/commit/66aca1b5e17571e849fbd8806f3f355ac83dc864))

## [0.1.0-dev.3](https://github.com/IMXEren/Suwayomi-Server-docker/compare/v0.1.0-dev.2...v0.1.0-dev.3) (2026-09-20)

### Features

* **deploy:** pin the bundled WebUI and headless runtime in Compose ([383773f](https://github.com/IMXEren/Suwayomi-Server-docker/commit/383773f4e0f8b007b543c9bb7d35cd67da0b9478))
* **deploy:** run FlareSolverr by default and enable it in the app ([c3b271b](https://github.com/IMXEren/Suwayomi-Server-docker/commit/c3b271b4e848eadefbf5150570abe84a02fc9b8f))

### Bug Fixes

* **deploy:** allow the rclone sidecar to mount over the bind-mounted path ([10eb9dd](https://github.com/IMXEren/Suwayomi-Server-docker/commit/10eb9ddfa4c741dd5042f495b4445e0c0d887610))

## [0.1.0-dev.2](https://github.com/IMXEren/Suwayomi-Server-docker/compare/v0.1.0-dev.1...v0.1.0-dev.2) (2026-09-20)

### Bug Fixes

* **ci:** authenticate the release lookup to avoid API rate limits ([6d8fd1e](https://github.com/IMXEren/Suwayomi-Server-docker/commit/6d8fd1e70f431064e501b87bed2a64c0fb3de6b9))
* **release:** publish the image as ghcr.io/imxeren/suwayomi-server ([e43a1f5](https://github.com/IMXEren/Suwayomi-Server-docker/commit/e43a1f5f5fc0db7e4ef981c87bd6f080cd4b8901))

## [0.1.0-dev.1](https://github.com/IMXEren/Suwayomi-Server-docker/compare/v0.0.0...v0.1.0-dev.1) (2026-09-20)

### Features

* **deploy:** make Compose own Suwayomi and rclone ([0c80d31](https://github.com/IMXEren/Suwayomi-Server-docker/commit/0c80d31a4094fafb3f7c5978a97621b55dec018b))

### Bug Fixes

* **release:** pass the release version to the image publisher ([17d3083](https://github.com/IMXEren/Suwayomi-Server-docker/commit/17d30831734153f1b43f3f838bba604bffca67d5))
