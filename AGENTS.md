# PaperForge workspace instructions

## Public deployment is part of completion

This repository serves the public instance at
`https://paperforge-linux.tail53ab99.ts.net`.

After every user-requested code change or optimization:

1. Run the relevant tests and static checks.
2. Deploy the change with `./scripts/dev restart` (or `./scripts/dev deploy`
   when a forced deployment is appropriate).
3. Treat the deployment command's Funnel target check and byte-for-byte public
   `/login` comparison as required acceptance checks.
4. Do not report the change as complete until the public URL is verified to be
   serving the new deployment. If deployment or public verification fails,
   report that explicitly and continue troubleshooting when it is safe to do so.

Do not stop or delete an older deployment that may still own an in-flight job.
Blue/green deployment gives every new worker a separate Redis database so old
jobs can finish safely.
