"""Reader for standard Unix `ar` archives (the real system `ar`/`ranlib`
create libgcc.a, since ar's own container format is a target-agnostic
envelope -- plain "global magic + per-member header + raw bytes", no ELF
or architecture-specific parsing involved. Our custom .o format
(symphony_obj.py, JSON) is simply archived as opaque member payloads.

This lets libgcc's real build (`ar rc libgcc.a *.o`) work unmodified while
our own symphony_ld.py extracts members from the resulting .a to pull in
whichever libgcc routines (__mulsi3 etc.) a program's undefined symbols
actually need -- the standard archive-linking semantics of "only pull in a
member if it defines something still undefined so far".
"""
GLOBAL_MAGIC = b"!<arch>\n"
HEADER_SIZE = 60


def read_archive(path):
    """Return a list of (member_name, payload_bytes), in archive order.
    Handles the GNU extended-filename table (`//` member) used whenever a
    member name is too long for the fixed 16-byte name field."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != GLOBAL_MAGIC:
        raise ValueError(f"{path}: not a Unix ar archive")
    pos = 8
    members = []
    long_names = b""
    while pos < len(data):
        if pos + HEADER_SIZE > len(data):
            break
        header = data[pos:pos + HEADER_SIZE]
        name_field = header[0:16].decode("ascii").rstrip()
        size_field = header[48:58].decode("ascii").strip()
        size = int(size_field)
        body_start = pos + HEADER_SIZE
        body = data[body_start:body_start + size]
        pos = body_start + size
        if pos % 2 == 1:
            pos += 1  # members are 2-byte aligned, padded with '\n'

        if name_field == "//":
            long_names = body
            continue
        if name_field == "/":
            continue  # symbol index (we don't need ranlib's index, see below)
        if name_field.startswith("/"):
            # GNU long-name reference: "/<offset>" into the "//" table.
            offset = int(name_field[1:])
            end = long_names.index(b"/\n", offset)
            real_name = long_names[offset:end].decode("ascii")
            members.append((real_name, body))
            continue
        members.append((name_field.rstrip("/"), body))
    return members
