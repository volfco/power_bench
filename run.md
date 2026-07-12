ansible-playbook -i ansible/hosts ansible/run_core_power_bench.yml --limit node2 -e power_bench_meter_mac=45:AF:4E:55:56:06 -e '{"power_bench_core_variants":["baseline"],"power_bench_repeats":1}'
