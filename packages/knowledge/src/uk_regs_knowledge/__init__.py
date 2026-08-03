"""Knowledge layer: persistence (object store, relational, graph) + embeddings."""

from uk_regs_core import (  # re-export for convenience
    ChangeSet,
    Citation,
    Document,
    DocumentVersion,
    Figure,
    Rule,
    RuleDelta,
)

from uk_regs_knowledge.embeddings import Embedder, OpenAIEmbedder, get_embedder
from uk_regs_knowledge.graph.connection import close_driver, get_driver
from uk_regs_knowledge.graph.graph_repo import GraphRepository
from uk_regs_knowledge.relational.connection import close_pool, get_pool, init_schema
from uk_regs_knowledge.repositories.relational_repo import (
    ChangeSetRepository,
    DocumentRepository,
    RuleRepository,
)
from uk_regs_knowledge.storage.object_store import (
    ObjectStore,
    S3ObjectStore,
    document_key,
    figure_key,
    get_object_store,
)

__all__ = [
    # repos
    "DocumentRepository",
    "RuleRepository",
    "ChangeSetRepository",
    "GraphRepository",
    # storage
    "ObjectStore",
    "S3ObjectStore",
    "get_object_store",
    "document_key",
    "figure_key",
    # connection helpers
    "get_pool",
    "close_pool",
    "init_schema",
    "get_driver",
    "close_driver",
    # embeddings
    "Embedder",
    "OpenAIEmbedder",
    "get_embedder",
    # re-exported models
    "Rule",
    "Citation",
    "Document",
    "DocumentVersion",
    "ChangeSet",
    "RuleDelta",
    "Figure",
]
