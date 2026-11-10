# CORE-02c: checks execute in disposable Docker workspaces

The host's working directory is not a security boundary. `run_checks` delegates to an operator-configured protocol1 runner; absent/invalid configuration fails closed.

`tools/docker_check_runner.py` supports Linux/WSL with an already available Docker daemon and immutable local image ID. It never starts Docker or pulls an image. Admin JSON config specifies `allowed_roots`, `docker` (absolute executable) and `image` (`sha256:...`). The configured argv may include `--config /absolute/admin.json`; job input cannot change these settings.

The runner validates a bounded snapshot beneath the managed root, rejects symlinks/special files and stale digests, mounts input read-only, and copies it into a disposable tmpfs workspace. Commands run without a shell wrapper as UID65534, with a minimal environment, no network, no capabilities, no privilege escalation, read-only image root, memory/CPU/PID/output limits and an explicit deadline. Temporary changes in the copy are expected; the host snapshot must retain its digest. The selected image itself is trusted operator-supplied tooling, so it must contain no credentials.

Docker create and attach/start are separate: a timed-out `docker run` client can race cleanup and leave a Created container. Cleanup targets only the generated container name and must be confirmed before a valid check result is returned. Container creation/daemon failures return a blocked result; the runner never falls back to executing the command on the host. An unresponsive daemon can require operator cleanup of a stopped container; it does not authorize another unisolated execution.

Linux paths work directly. An operator may additionally configure `windows_roots` as a list of `{source: "R:\\ManagedSnapshots", target: "/mnt/r/ManagedSnapshots"}` mappings for the same machine's WSL mounts. Windows paths require an explicit unambiguous mapping, and the resolved Linux path must still pass the managed-root/containment/digest checks. There is no implicit access to laptop snapshots from the server and no general Windows sandbox claim. Input currently limited to48MiB and20,000 files; ephemeral workspace64MiB. Missing tools/dependencies are real check failures, not PASS. Profiles with no configured supported runner remain unavailable for automatic checks.

Real integration verification: success, real exit7 failure, writable ephemeral copy with unchanged source, outside-host marker unavailable, environment marker absent, UID65534, read-only input/root, network refusal, NoNewPrivs1, timeout termination, bounded output, stale/out-of-scope/symlink rejection and no test containers left.

Run on an isolated Linux host with an existing credential-free Python image:

```sh
AIHUB_RUNNER_TEST_IMAGE=sha256:<existing-image-id> python3 tests/test_docker_check_runner.py
```

This is execution isolation for checks. It is separate from filesystem patch journaling, resource fencing and model-provider sandbox capabilities.
