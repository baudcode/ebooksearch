REGISTRY  ?= tower.local:5000
IMAGE     ?= ebooksearch
# Read the canonical version straight from pyproject.toml.
VERSION   := $(shell awk -F'"' '/^version *= */ {print $$2; exit}' pyproject.toml)
TAG       ?= v$(VERSION)
PLATFORMS ?= linux/amd64,linux/arm64
REF       := $(REGISTRY)/$(IMAGE):$(TAG)
REF_LATEST := $(REGISTRY)/$(IMAGE):latest
BUILDER   := ebooksearch-builder

.PHONY: all build push release run clean builder version

all: build

version:
	@echo $(VERSION)

# Multi-arch build + push in one step (buildx requires this — the resulting
# manifest list cannot be loaded into the local Docker daemon directly).
# Pushes both `:vX.Y.Z` (or whatever TAG is) and `:latest` from the same build.
build: builder
	docker buildx build \
		--builder $(BUILDER) \
		--platform $(PLATFORMS) \
		-t $(REF) \
		-t $(REF_LATEST) \
		--push \
		.

# `make push` is a synonym for `make build` (buildx pushes as part of build).
push: build

# `make release` is the conventional name for the same thing.
release: build

# Local single-arch build for development — loads into the local daemon so
# `make run` works.
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
