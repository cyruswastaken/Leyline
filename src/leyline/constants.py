"""Values shared by the schema and the configuration."""

# Size of every stored embedding. Fixed in the schema so an approximate-nearest-
# neighbour index can be built. 768 suits nomic-embed (native) and the OpenAI
# text-embedding-3 family (which can be asked for 768). Changing it requires a
# migration and a full re-index.
EMBEDDING_DIMENSIONS = 768
