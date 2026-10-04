# Third-party notices

AI Gateway is MIT-licensed (see `LICENSE`, copyright AOX LLC). It uses the third-party material below. Harborline Supply Co. and all of its data are fictional. The AOX logo in `dashboard/public/brand/` is AOX's own mark.

This file covers what the dashboard ships, the fonts and icons it carries, and the database image the stack pulls. It does not reproduce the licences of the gateway's Python dependencies (they are pinned in `uv.lock`) or of the handbook server's embedding model (`minishlab/potion-base-8M`, MIT, pinned by hash in `scripts/potion-base-8M.sha256`).

## Fonts: IBM Plex Sans, IBM Plex Mono and Space Grotesk (SIL Open Font License 1.1)

The dashboard self-hosts these three families (nothing is fetched from a font service). Each family's licence text sits beside its font files as `OFL.txt`:

| Family | Files | Copyright | Licence text |
| --- | --- | --- | --- |
| IBM Plex Sans | `dashboard/src/fonts/ibm-plex-sans/*.woff2` (Regular, Italic, Medium, SemiBold; Latin 1 subset) | Copyright © 2017 IBM Corp. | `dashboard/src/fonts/ibm-plex-sans/OFL.txt` |
| IBM Plex Mono | `dashboard/src/fonts/ibm-plex-mono/*.woff2` (Regular, Medium; Latin 1 subset) | Copyright © 2017 IBM Corp. | `dashboard/src/fonts/ibm-plex-mono/OFL.txt` |
| Space Grotesk | `dashboard/src/fonts/space-grotesk/*.woff2` (500, 600) | Copyright 2020 The Space Grotesk Project Authors (https://github.com/floriankarsten/space-grotesk) | `dashboard/src/fonts/space-grotesk/OFL.txt` |

The Plex texts are IBM's own `LICENSE.txt` (https://github.com/IBM/plex) and the Space Grotesk text is the project's `OFL.txt`, fetched on 2026-10-03. The dashboard image carries the three texts, and the Tabler Icons MIT text, under `/app/licenses/`, so they travel with the font files.

**Provenance.** The six IBM Plex `.woff2` files are IBM's own released files, unmodified, with IBM's own file names (`IBMPlexSans-{Regular,Italic,Medium,SemiBold}-Latin1.woff2` and `IBMPlexMono-{Regular,Medium}-Latin1.woff2`). They come from the `fonts/split/woff2/` folder of IBM's npm packages `@ibm/plex-sans` 1.1.0 and `@ibm/plex-mono` 2.5.0 (https://github.com/ibm/plex, licence `OFL-1.1`), and IBM's `LICENSE.txt` from each package sits beside them as `OFL.txt`. IBM publishes the Latin 1 subset itself, so nothing was converted or subset by AOX or by a third party, and the Reserved Font Name "Plex" is not in question. SHA-256 of each file, as shipped:

| File | SHA-256 |
| --- | --- |
| `IBMPlexSans-Regular-Latin1.woff2` | `b5ad7bd39f996144915f0ad9849a90183b27d8c28ad97ed98af5b1bebc51f6b1` |
| `IBMPlexSans-Italic-Latin1.woff2` | `0a06b98143f3453b81f3c396241a01c6c4cff84c1a77bf0c75b18bd603018506` |
| `IBMPlexSans-Medium-Latin1.woff2` | `b5610af04d0d4b5a14a621d96d974b993e945a065db1a8861918f69ef9321934` |
| `IBMPlexSans-SemiBold-Latin1.woff2` | `fff0ab3a88b0b4aa0b693e4f0201359a15183b08e3fa5696d1918d8f0ade8ad5` |
| `IBMPlexMono-Regular-Latin1.woff2` | `e8993d946649b9d01abb1ed06d574b19d8ea3e66b5c3948602db335c44c18e56` |
| `IBMPlexMono-Medium-Latin1.woff2` | `41201b658a328b9d00368215c2f1102770f80b15952ab82631e4006255e6365d` |

The Latin 1 subset covers every non-ASCII character the dashboard's source uses (the middle dot, the em dash and the ellipsis); a test checks the file list. The Space Grotesk files are the Fontsource 5.3.0 `latin` subset (byte-identical to `@fontsource/space-grotesk`); Space Grotesk declares no Reserved Font Name. This is not legal advice.

## Icons: Tabler Icons (MIT)

The outline icons in `dashboard/src/components/Icon.tsx` (activity, list, clock, check, ban, alert, info, x, sun, moon, logout, chevrons, inbox, coin, refresh) follow the Tabler Icons set (https://github.com/tabler/tabler-icons) in the UI system's style, and several paths are taken or adapted from it.

```
MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## The database image: pgvector on PostgreSQL

`compose.yaml` and the CI workflow pull `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` from Docker Hub. AOX does not modify or redistribute it. As inspected on 2026-10-03 it holds PostgreSQL 17.11 on Debian 12 (bookworm) with pgvector 0.8.7.

- **PostgreSQL** is under the PostgreSQL License (https://www.postgresql.org/about/licence/), copyright PostgreSQL Global Development Group and the Regents of the University of California. Source: https://www.postgresql.org/ftp/source/.
- **pgvector** is under the PostgreSQL License, copyright PostgreSQL Global Development Group and the Regents of the University of California. Source: https://github.com/pgvector/pgvector. Its licence text:

```
Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group

Portions Copyright (c) 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement
is hereby granted, provided that the above copyright notice and this
paragraph and the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR
DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING
LOST PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS
DOCUMENTATION, EVEN IF THE UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS FOR A PARTICULAR PURPOSE.  THE SOFTWARE PROVIDED HEREUNDER IS
ON AN "AS IS" BASIS, AND THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO
PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
```

- **The Debian packages** in the image (about 145) are each under their own licence, in `/usr/share/doc/<package>/copyright` inside the image; Debian's sources are at https://sources.debian.org/. Some are copyleft (GPL and LGPL libraries); nothing from them is linked into AOX's own code.

## The dashboard's npm packages (production)

What the dashboard image carries is `.next/standalone/node_modules` plus the build output: 31 packages as built on 2026-10-03, by licence:

| Licence | Packages |
| --- | --- |
| MIT | next, @next/env, react, react-dom, styled-jsx, postcss, nanoid, client-only, pg, pg-pool, pg-protocol, pg-types, pg-connection-string, pg-cloudflare, pgpass, postgres-array, postgres-bytea, postgres-date, postgres-interval, xtend |
| Apache-2.0 | @swc/helpers, baseline-browser-mapping |
| BSD-3-Clause | source-map-js |
| ISC | picocolors, pg-int8, split2 |
| CC-BY-4.0 | caniuse-lite (browser-support data, © Alexis Deveria and the caniuse-lite contributors, https://creativecommons.org/licenses/by/4.0/) |

`sharp` and its LGPL-licensed libvips, which Next traces into its output as an optional image optimiser, are deliberately kept out of the image (`outputFileTracingExcludes` in `dashboard/next.config.ts`): nothing here optimises images. The development tools (TypeScript, ESLint, Vitest and their dependencies) are not shipped. To list the packages and licences again, from `dashboard/`: build, then read the `license` field of each `package.json` under `.next/standalone/node_modules` (CI runs `npm audit` on the production tree, not a licence check).
