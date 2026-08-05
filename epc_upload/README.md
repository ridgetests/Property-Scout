# Drop your EPC download here

This folder is where you put the EPC data you download **by hand** from the
government site, to switch on **size-matched valuations** (the "4 nearby homes of
a similar size" method).

Why by hand: the government moved the EPC bulk service behind a login, so it can't
be fetched automatically anymore.

## What to do (once, whenever you have the patience)

1. Sign in and download the **domestic EPC certificates** for your councils
   (Waverley, and East Hampshire) from the government's EPC data service
   (get-energy-performance-data.communities.gov.uk → download all data). You'll
   need a free GOV.UK One Login.
2. Upload whatever it gives you (a `.zip` or a `certificates.csv`) into **this
   folder** (`epc_upload/`) using GitHub's **Add file → Upload files**.
   - GitHub's web upload limit is 25 MB per file. A single council's zip is
     usually well under that. If a file is too big, tell me and we'll split it.
3. Run the button: **Actions → "Build house-size file from upload" → Run workflow → main**.

That builds `epc_region.json.gz` and saves it into the project. The next nightly
update then values properties against homes of a similar floor area.

You can delete the files from this folder afterwards if you like — the built
`epc_region.json.gz` at the project root is the thing that matters.
