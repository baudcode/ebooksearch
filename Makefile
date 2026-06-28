REGISTRY  ?= ghcr.io/baudcode
IMAGE     ?= ebooksearch
# Read the canonical version straight from pyproject.toml.
VERSION   := $(shell awk -F'"' '/^version *= */ {print $$2; exit}' pyproject.toml)
TAG       ?= v$(VERSION)
PLATFORMS ?= linux/amd64,linux/arm64
REF       := $(REGISTRY)/$(IMAGE):$(TAG)
REF_LATEST := $(REGISTRY)/$(IMAGE):latest
BUILDER   := ebooksearch-builder

.PHONY: all build push release local build-local run clean builder version

all: build

version:
	@echo $(VERSION)

# Multi-arch build + push to the configured REGISTRY (default: ghcr.io).
# Pushes both `:vX.Y.Z` (or whatever TAG is) and `:latest` from one build.
build: builder
	docker buildx build \
		--builder $(BUILDER) \
		--platform $(PLATFORMS) \
		-t $(REF) \
		-t $(REF_LATEST) \
		--push \
		.

# `make push` and `make release` are synonyms for `make build`.
push: build
release: build

# Push to the LAN registry instead of ghcr.io. Useful for testing on a NAS
# without going through GitHub.
local:
	$(MAKE) build REGISTRY=tower.local:5000

# Single-arch local-daemon build for `make run`.
build-local:
	docker build -t $(REF) .

run: build-local
	docker run --rm -p 8000:8000 -v $(PWD)/test-ebooks:/data/books $(REF)

builder:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 || \
		docker buildx create --name $(BUILDER) --driver docker-container \
			--config buildkitd.toml --use

clean:
	-docker buildx rm $(BUILDER)
	-docker rmi $(REF)
