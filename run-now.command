#!/bin/zsh
# Double-click in Finder to summarise every new book in the input folder now (the same as ./run.sh).
cd "${0:A:h}"
./run.sh
code=$?
echo; read -k1 -s "?Finished (exit $code; details in work/<book>/run.log). Press any key to close."
