**DRAFT for counsel review. Not legal advice. Not in effect.**

# Questions for counsel

Before OmniFile Gateway's first public release, and before any outside contribution is accepted (decision
25). Each question says what we have now and what we need to know. The facts come from the repository:
`LICENSE`, `LICENSES.md`, `docs/DECISIONS.md` (decisions 25, 36, 38 and 46 to 48), `appliance/README.md`
("Source offer"), `appliance/source-offer/OFFER.md.in` and `samba-vfs/README.md`.

**What OmniFile Gateway is, in short.** Two parts, both delivered to a client:

- **The service**, container images the client runs in its own Azure subscription. Our code (Python,
  Business Source License 1.1) with open source dependencies listed in `LICENSES.md`.
- **The edge appliance**, a virtual machine disk image (Ubuntu 26.04 LTS, built by us with mkosi) that the
  client runs in its data centre. It holds Ubuntu packages (among them GPL-2 and GPL-3 software: the Linux
  kernel, Samba, parts of systemd, GnuPG), our own build of NFS-Ganesha (LGPL-3.0) with our module
  `FSAL_OMNI` (`fsal/`, LGPL-3.0), our Samba module `vfs_omnifile_prevver` (`samba-vfs/`, GPL-3.0-or-later),
  and our edge daemon and tools (Rust, BSL 1.1). Its `/usr` is read-only and verified (dm-verity), and its
  updates are signed with our key (decision 47); Secure Boot is optional (decision 48).

## 1. Contributions

1.1 **Which form.** We drafted an individual and an entity contributor licence agreement in our own words
(`individual-cla.md`, `entity-cla.md`). Should we use a published standard form instead (for example the
Apache Software Foundation's ICLA and CCLA, or a Project Harmony template), adapted to the BSL? Which is
safer for a licensor that sells commercial licences?

1.2 **Licence or assignment.** The drafts take a broad licence, with the right to relicense under any
terms, and leave the copyright with the contributor. Is that enough to sell commercial licences and to
change the licence of later versions, in the jurisdictions our clients are in? Or do we need a copyright
assignment (with a licence back)?

1.3 **Signing.** Is a click-through agreement bound to a GitHub account (a "CLA assistant" bot recording
acceptance on a pull request) enforceable enough, or do we need an electronic signature service, or wet
signatures? For an employee, when must the employer sign the entity agreement?

1.4 **DCO instead.** `dco.md` explains why we think a Developer Certificate of Origin alone doesn't give
the rights decision 25 needs. Do you agree? Would a DCO sign-off plus a contribution licence grant written
into `CONTRIBUTING.md` be an acceptable, lower-friction alternative?

1.5 **The GPL and LGPL directories.** Contributions to `samba-vfs/` (GPL-3.0-or-later) and `fsal/`
(LGPL-3.0) are distributed under those licences. Section 2 of the drafts also lets us license them on
other terms. Is that sound, and is it worth having? (We have no plan to sell those two modules under
other terms; we may want the option.) Should the agreement treat them differently?

1.6 **Governing law and venue** for the agreements: which to choose.

## 2. The Business Source License 1.1 and its Additional Use Grant

2.1 **The grant's wording.** `LICENSE` grants production use to organisations that "together with your
affiliates, have fewer than 100 total employees and contractors and less than US$1,000,000 in total revenue
in your most recent fiscal year" (decision 25). Please review the wording, in particular:

- how "affiliates", "contractors" and "revenue" are counted, and for a group of companies;
- non-profits, universities and government bodies, which may have no "revenue";
- when an organisation grows past a threshold: from when it needs a commercial licence, and what happens
  to the versions it already runs;
- who the "you" is when a systems integrator (such as Prizmanyx's operator) deploys OmniFile for a client:
  the integrator, the client, or both;
- whether running the service in the client's own Azure subscription, for the client's own staff, is
  "production use" by the client alone.

2.2 **Change Date and Change License.** Each version converts to the Apache License 2.0 four years after
it is published. Is "the date the Licensed Work is published" precise enough per version (we tag
releases)? Must each file, or each release, state its Change Date?

2.3 **The licence text and our exceptions.** `LICENSE` says the Licensed Work excludes `fsal/` (LGPL-3.0),
but it doesn't mention `samba-vfs/` (GPL-3.0-or-later, decision 36), which `LICENSES.md` lists as an
exception. Should `LICENSE` name it too? Is the exclusion wording right for each?

2.4 **Commercial licence terms.** Decision 25 asks counsel to draft them; we have none yet.

2.5 **Describing it.** We say "source-available, not open source". Is there anything else to avoid in
how we describe the licence?

## 3. `samba-vfs/`: our GPL-3 Samba module

`vfs_omnifile_prevver` is a Samba VFS module (C, GPL-3.0-or-later), built inside the matching Samba
release (`samba-vfs/build.sh`) and loaded by smbd as a shared object. It fills a file opened directly in a
point-in-time ("Previous Versions") folder. It is never linked into any of our BSL binaries, and it talks to
no OmniFile process: it only opens folders on the local filesystem, which our daemon (a separate process)
reacts to.

3.1 Is the BSL edge daemon a separate work from this module and from Samba, given that the only
interaction is through the filesystem? Does anything in the module's design make it otherwise?

3.2 We ship the module's binary in the appliance image, and its source in the source offer. Is anything
else needed (notices, the GPL text in the image, an offer for the module specifically)?

3.3 As the copyright holder we license the module under GPL-3.0-or-later. Should it be GPL-3.0-only?

## 4. `fsal/`: our LGPL-3 NFS-Ganesha module

`FSAL_OMNI` (C, LGPL-3.0) is a module NFS-Ganesha (LGPL-3.0) loads. It talks to our BSL edge daemon over a
Unix socket (`SOCK_SEQPACKET`, `docs/contracts/fsal-socket.md`). We build Ganesha ourselves, with the
module, and ship it in the appliance image.

4.1 Is the daemon a separate work from Ganesha and `FSAL_OMNI`, given the socket boundary?

4.2 The LGPL-3 (through GPL-3 section 6) asks for "Installation Information" when object code is conveyed
in a "User Product". The appliance's `/usr` is read-only and verified, and updates must be signed with our
key, so a client can't replace our Ganesha build in place. Is a virtual appliance sold to businesses a
"User Product"? If it could be, what must we provide (for example a documented way to build and boot an
image with a modified Ganesha, or to enrol the client's own update key)? The same question applies to the
GPL-3 programs in the image (Samba, GnuPG, and the parts of systemd under the LGPL).

## 5. Redistributing the Azure Arc agent in the appliance image

The appliance can be managed through Azure Arc (its Connected Machine agent, `azcmagent`), which gives it a
managed identity and ships its logs. Today the image does not contain the agent: whether to bake it into
the read-only `/usr`, ship it as a separate system extension, or have the client install it from Microsoft
is an open owner decision (`docs/design/appliance-image.md`, "Step 3, Arc").

5.1 Do Microsoft's licence terms for the Connected Machine agent let us include it in an image we
distribute to clients? If so, on what conditions (notices, versions, updates, the client's own agreement
with Microsoft)?

5.2 If not, is it enough that our setup steps download and install it from Microsoft at the client's
request, under the client's own Azure agreement?

5.3 The same, briefly, for the other third-party binary we ship: AWS's IAM Roles Anywhere signing helper
(Apache-2.0; we ship its LICENSE and NOTICE, `LICENSES.md`). We believe that one is fine; please confirm.

## 6. The GPL source offer (decision 46)

What we built: for each appliance release, `appliance/source-offer/` collects the complete corresponding
source of every GPL and LGPL package in the image (the exact Ubuntu source packages from Ubuntu's snapshot
archive for the build date, the pinned Samba, NFS-Ganesha and libntirpc sources, and our two modules), with
checksums and a manifest. `publish_sources.py` publishes it as a GitHub Release in a public repository,
`omnifile-gateway-sources`, one release per appliance release. The written offer (`OFFER.md.in`) points to
that release, offers a physical copy for three years at no more than the cost of distribution, and takes
requests through the repository's GitHub issues. The appliance image itself is delivered to clients
separately (not from that repository).

6.1 For the GPL-2 parts (the kernel above all), which allow only accompanying source or a written offer
(GPL-2 section 3(a) or 3(b)), is our written offer valid as drafted? Is "open an issue in this public
repository" an acceptable way to receive requests, or must the offer give a postal or email address
(`source_offer.py --contact` adds a postal address)?

6.2 For the GPL-3 and LGPL-3 parts, does publishing the source in a public GitHub Release count as offering
equivalent access (GPL-3 section 6(d)), given that the image is downloaded from somewhere else? What must
the place the image is downloaded from say?

6.3 How long must each release's source stay published, and what if GitHub, or the repository, became
unavailable? Should we keep our own copy and say so?

6.4 Where must the offer appear: in the image itself (today it is not), with the download, in the
client's contract? And must the image carry each package's licence and copyright file, which a minimized
image may leave out?

## 7. Also worth a look

7.1 **Export control.** The service and the appliance use TLS and Key Vault-backed signing. Do encryption
export rules apply to distributing the images and the source, and to which clients?

7.2 **The name.** Has "OmniFile Gateway" been cleared as a trade mark in the markets we sell in?

7.3 **Customer data.** OmniFile stores file names, AD identities and access logs in the client's own
Azure. Anything our documentation should say about the client being the controller and us not having
access?
