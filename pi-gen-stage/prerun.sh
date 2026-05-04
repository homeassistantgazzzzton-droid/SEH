#!/bin/bash -e
# Prerun pour le stage SEH — ne fait rien de spécial, on hérite du rootfs précédent
if [ ! -d "${ROOTFS_DIR}" ]; then
  copy_previous
fi
