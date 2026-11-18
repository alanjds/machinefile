# basic

Kitchen-sink example: ARG/ENV expansion, multi-line RUN continuation,
COPY with mode preservation (the udev rule), ADD from a URL, WORKDIR
context on later RUNs, and USER switching (`sudo -u ubuntu`).

Compile it:

    machine examples/basic/Machinefile

The COPY'd udev rule lives in `config/`; its sha1 checksum is embedded in
the generated step, so editing the rule changes that step and everything
after it (layer-cache semantics).
