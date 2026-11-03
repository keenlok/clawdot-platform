# chatcore-research

Bounded inline and durable background web research for chatcore. It uses the existing
`chatcore-web` search and fetch tools, gives the model only read tools, and renders citations and
source links in the application rather than trusting model supplied URLs.

`ResearchAccess` is the authorization seam for user restrictions. The app currently passes
`AllowAllResearch`; replace it with the per-user restrictions adapter when that store lands.
`MemoryResearchJobs` supports local development but loses jobs on restart. Postgres jobs use
leases and delivery reservations; sends with unknown outcomes are never replayed.
