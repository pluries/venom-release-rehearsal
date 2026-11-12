# The site image

This folder packages the venom documentation site as a container image: the site is built
from the repository, then served by nginx as a non-root user.

## Build

From the repository root:

```bash
docker build -f deploy/site/Dockerfile -t venom-site .
```

## Run

The image works with a read-only root file system; nginx writes only under `/tmp`.

```bash
docker run --rm --read-only --tmpfs /tmp -p 8080:8080 venom-site
```

Then open `http://localhost:8080/`. The licence is in the image at `/licenses/LICENSE`.

## Search and llms.txt

Every build writes three more files at the site root: `search-index.json` (read by the search box in the top bar),
`llms.txt` (every page with its address and a one-line description) and `llms-full.txt` (the text of every page).
The addresses come from `baseUrl` in `site/site.json`; a build without one stops with an error.
The search runs in the browser and fetches only the site's own index, so the Content-Security-Policy in `nginx.conf`
allows `connect-src 'self'` and nothing wider. The text files are served as utf-8 (`charset utf-8;`).
`scripts/check-site.sh` checks all three files on the live host.

## Refresh the base images

Both base images are pinned by digest alone. To move to a newer build:

```bash
docker buildx imagetools inspect python:3.12-alpine
docker buildx imagetools inspect nginxinc/nginx-unprivileged:1.28-alpine
```

Put the top-level `Digest:` of each into its `FROM` line, and update the version comment above it.

## Publish

The Jenkins job `venom-publish` is one call of the shared library's `imMirrorPublishPipeline` (`jenkins/publish.Jenkinsfile`). After a green `main` build the library runs the dry run, asks the maker to approve, publishes to GitHub, and only then publishes the site. The site steps are command scripts that the library calls:

- `jenkins/publish-steps.sh` (`setup`, `dry-run`, `publish`) wraps the GitHub publish.
- `jenkins/image-steps.sh` (`build`, `push`) proves the image from the repository root and pushes it by digest.
- `jenkins/gitops-steps.sh` (`update`) sets that digest in the private GitOps repository.

`scripts/check-site.sh` checks the deployed site from outside. It verifies:

- the home page answers 200 and carries the site marker, sets no cookie and sends the Content-Security-Policy;
- the certificate has more than 30 days left, and `http` answers 301 to the same path over `https`;
- `search-index.json`, `llms.txt` and `llms-full.txt` answer 200, the two text files as utf-8;
- the server refuses what is not the site: `POST /`, `/.git/config` and the directory `/fonts/` must not answer 2xx (a redirect counts as refused; the check does not follow it).

The job file and its definition are not committed yet: the library's commit scan needs a patch the maker applies first (`docs/feat-breakdown/3680/commit-scan.md`). Nothing has run in Jenkins. The scripts and the check are in place.

Every cluster setting lives in that private GitOps repository, or in the job's parameters and credentials. None of them is kept in this folder.

To check the deployed site from outside, run:

```bash
scripts/check-site.sh
```

To roll back, put the previous digest back in the private GitOps repository.
