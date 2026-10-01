# curl

Optional for service operation. Useful for health checks, API diagnostics, and upstream installers that download scripts or binaries.

## Prerequisites and installation

Install an OS package or follow the [official download page](https://curl.se/download.html). HTTPS requests need the platform's trusted CA certificates and appropriate network access.

```bash
curl --version
curl --noproxy '*' http://127.0.0.1:8765/health
```

For protected endpoints, follow the [service authentication instructions](../README.md#run). The public health endpoint alone does not establish backend authentication or successful review publication.
