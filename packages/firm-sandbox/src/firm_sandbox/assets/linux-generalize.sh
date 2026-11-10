#!/bin/bash
set -euo pipefail
control_user=${SUDO_USER:?}
cloud-init clean --logs --machine-id --configs all --seed
# The control account still owns this final SSH session.
for account in "$control_user" ubuntu; do
    if id "$account" >/dev/null 2>&1; then userdel --force --remove "$account"; fi
done
rm -rf /home/* /root/* /root/.[!.]* /root/..?* /tmp/* /var/tmp/*
rm -rf /var/lib/cloud /var/lib/samba/* /var/cache/samba/*
rm -rf /var/lib/docker /var/lib/containerd /var/lib/private
rm -f /etc/ssh/ssh_host_* /etc/ssl/private/ssl-cert-snakeoil.key
rm -f /etc/ssl/certs/ssl-cert-snakeoil.pem /var/lib/dbus/machine-id
rm -f /etc/sudoers.d/90-cloud-init-users
rm -f /etc/netplan/* /usr/sbin/policy-rc.d
rm -rf /var/lib/dhcp/* /var/lib/systemd/network/*
rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/*
# Keep the package-owned log directories and their permissions for the next boot.
find /var/log -type f -delete
find /var/log -type l -delete
rm -rf /var/log/journal/*
fstrim -av
sync
