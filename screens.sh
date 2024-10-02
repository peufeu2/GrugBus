for s in `screen -ls | grep Detached`; do screen -r $s; done

