# Compatibility

Works against any NAS running Synology Photos (DSM 7.x). API versions are discovered at runtime (`SYNO.API.Info`), not hardcoded — the Person/Item endpoints vary across DSM releases. The write-back chain (`Person.add_face` v3 + `Upload.Face`) and the deletion chain (`BackgroundTask.File.delete`) were captured against a current DSM; if your DSM predates them, everything read-only still works and the write commands will fail loudly rather than corrupt anything.
