ansible-playbook -i ansible/hosts ansible/run_core_power_bench.yml --limit node2 -e power_bench_meter_mac=45:AF:4E:55:56:06 -e '{"power_bench_sweep":"amd","power_bench_only":["stack=amd_performance+pcie_aspm"],"power_bench_skip_baseline":true,"power_bench_repeats":1}'

# Configure the opt-in local memory suite and pre-install its profiles.
ansible-playbook -i ansible/hosts ansible/setup_phoronix.yml --limit node2 -e pts_install_memory_suite=true

# Run the configured memory suite with the standard power meter harness.
python3 run_suite.py 192.168.1.76 --user metrolla --ansible-limit node2 --sweep amd --tests local/power-bench-memory-1.0.0 --only baseline --repeats 1 --initial-reboot --mac 45:AF:4E:55:56:06 --checksum-policy warn

# Pre-install the opt-in official PTS suite collections before measuring them.
# Override pts_extended_suites with a JSON list to install only a subset.
ansible-playbook -i ansible/hosts ansible/setup_phoronix.yml --limit node2 -e pts_install_extended_suites=true

# Run the selected official suites (disk, database, audio encoding, browsers).
python3 run_suite.py 192.168.1.76 --user metrolla --ansible-limit node2 --sweep amd --tests pts/disk pts/database pts/audio-encoding pts/browsers --only baseline --repeats 1 --initial-reboot --mac 45:AF:4E:55:56:06 --checksum-policy warn

# Security-sensitive kernel-parameter experiments are explicit and temporary; the
# runner reconciles its managed GRUB fragment to stock settings when the sweep ends.
python3 run_suite.py 192.168.1.76 --user metrolla --ansible-limit node2 --sweep amd --only kernel_params=mitigations_off+nokaslr --repeats 1 --initial-reboot --mac 45:AF:4E:55:56:06 --checksum-policy warn
