"""The lab upstream: a lenient MCP server whose tool definitions change between phases.

TEST TOOLING for the red-team scorecard, never part of the product. It exists to be attacked: a
tool whose description or schema changes after review (a rug pull), tools nobody reviewed, a server
that accepts what its own schema forbids. It runs only in the `lab` Compose profile, only with
`LAB_MUTABLE_UPSTREAM=yes`, and only with a credential given for the run. Harborline Supply Co. is
fictional, and so is everything this server says.
"""
