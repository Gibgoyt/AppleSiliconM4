#!/bin/bash

current_dir="$PWD"

sudo -E env "PATH=$PATH" \
	M1N1DEVICE=/dev/ttyACM0 \
	python3 $current_dir/upload_and_call.py build/kernel.bin
