# Platform build notes

Cleaned notes of the commands used to prepare each device for the experiments in the dissertation
*Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101).

- All devices run **Ubuntu 24.04.5 LTS** (64-bit) except the Mac mini (macOS, not covered here).
- All devices use **OpenSSL 3.5.8**, built from source (default provider, native ML-KEM / ML-DSA, no oqs-provider).
- The lab network is isolated (192.168.55.0/24, no internet during measurement). Radios (Wi-Fi, Bluetooth) and IPv6 are disabled on every device.
- Notes were written during the build. Some steps are deliberately repeated (e.g. `netplan apply` / `cloud-init clean`) because ordering mattered on some images.
- `<user>` stands for the experiment account created on each device.

Lab addresses used in these notes: Collector `192.168.55.200`, DUT-PC `192.168.55.17`, DUT-RISCV `192.168.55.15`, jump host / NTP `192.168.55.100`.

---

## 1. Collector (Raspberry Pi 5)

Image: Raspberry Pi Imager, *Other general-purpose OS*, *Ubuntu*, **Ubuntu Server 24.04.5 LTS (64-bit)**.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-pip python3-serial
pip install --break-system-packages pyserial pycryptodome
```

Static address, wired only. Edit `/boot/firmware/network-config`, removing everything else (IPv6, Wi-Fi):

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      optional: true
      addresses: [192.168.55.200/24]
```

```bash
sudo cloud-init clean --logs
echo "dtoverlay=disable-wifi" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Check all radios are off:

```bash
systemctl status bluetooth      # Unit bluetooth.service could not be found.
ip link show wlan0              # Device "wlan0" does not exist.
```

Power meter: connect the TC66C (micro-USB) to a USB 2 port on the Collector and confirm it is detected:

```bash
ls -l /dev/serial/by-id/
# usb-RuiDeng_TC66_USB_Type-C_Meter_TC66-if00 -> ../../ttyACM0
```

Common additions for the Collector account, time source, and file-transfer tools:

```bash
sudo adduser <user>
sudo usermod -aG adm,dialout,cdrom,sudo,dip,lxd <user>     # same extra groups as the default account
su - <user>
sudo whoami; whoami                                        # root, then <user>

git --version
rsync --version | head -1

# Time: point systemd-timesyncd at the jump host / NTP server
sudo sed -i 's/^#NTP=.*/NTP=192.168.55.100/' /etc/systemd/timesyncd.conf
sudo systemctl restart systemd-timesyncd
sleep 5
timedatectl timesync-status
timedatectl show-timesync --all
```

---

## 2. DUT-Pi5 and DUT-CM5 (Raspberry Pi 5, Compute Module 5)

### Raspberry Pi 5

Image Ubuntu Server 24.04.5 LTS to the SD card with Pi Imager (USB or an SSD would also work).

```bash
sudo vi /boot/firmware/user-data     # set hostname if not already set in the Imager customisation
sudo cloud-init clean --logs
sudo reboot

sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-pip python3-serial
pip install --break-system-packages pyserial pycryptodome

ip a show eth0                       # can SSH to this address before the static one is applied
```

### Compute Module 5

Get the image onto the board by holding the boot button while applying USB-C power (the LED is red rather than green). Install `rpiboot` and run it once the LED is red; the board's eMMC (`mmcblk0`) then appears as a selectable target. After first boot, find the address from the DHCP server and SSH in. `eth0` is the outermost interface; the central one (`enp1s0`) is shut down at boot.

### OpenSSL 3.5.8 (all Linux DUTs)

```bash
sudo apt install -y build-essential checkinstall zlib1g-dev
wget https://www.openssl.org/source/openssl-3.5.8.tar.gz
tar -xzf openssl-3.5.8.tar.gz
cd openssl-3.5.8
./config --prefix=/usr/local/ssl --openssldir=/usr/local/ssl shared zlib
sudo make -j1      # single-threaded: -j has caused build problems on some ARM / RISC-V boards
sudo make install
export PATH="/usr/local/ssl/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/ssl/lib:$LD_LIBRARY_PATH"
```

Build time on the Pi: about 15 minutes or more for `make`, 3-4 minutes for `make install`.

Validation:

```bash
openssl version
openssl list -kem-algorithms | grep -i ml
openssl list -signature-algorithms | grep -i ml

openssl genpkey -algorithm ML-KEM-768 -out /tmp/test.key && openssl pkey -in /tmp/test.key -text -noout | head -5
rm /tmp/test.key

openssl speed rsa2048
openssl speed ML-KEM-768
```

Expected: `OpenSSL 3.5.8`; ML-KEM-512/768/1024 and the hybrids X25519MLKEM768, X448MLKEM1024, SecP256r1MLKEM768, SecP384r1MLKEM1024 listed `@ default`; ML-DSA-44/65/87 listed `@ default`.
Pi 5 reference result: RSA-2048 about 282 signs/s; ML-KEM-768 about 17,900 keygens/s.

### Make OpenSSL 3.5.8 the default for every account

```bash
sudo tee /etc/profile.d/openssl-local.sh > /dev/null <<'EOF'
export PATH=/usr/local/ssl/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/ssl/lib:$LD_LIBRARY_PATH
EOF
sudo chmod +x /etc/profile.d/openssl-local.sh
# log out and in again, then:
openssl version -a
```

### Experiment account

```bash
groups admin                       # note the default account's groups
sudo adduser <user>
sudo usermod -aG adm,cdrom,sudo,dip,lxd <user>
su - <user>
sudo whoami; whoami                # root, then <user>
openssl version -a                 # confirm 3.5.8 is the default for this account
```

### Network lock-down (final step, wired static address only)

```bash
sudo vi /boot/firmware/network-config
```

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      optional: true
      link-local: []
      addresses: [192.168.55.<n>/24]
```

```bash
echo "dtoverlay=disable-wifi" | sudo tee -a /boot/firmware/config.txt
echo "dtoverlay=disable-bt"   | sudo tee -a /boot/firmware/config.txt

# IPv6 off
sudo tee /etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
sudo sysctl --system

# applied more than once on purpose: ordering mattered on some images
sudo netplan apply
sudo cloud-init clean --logs
sudo netplan apply
sudo cloud-init clean --logs
sudo reboot
```

Check radios are off, IPv6 is off, and the address is correct:

```bash
systemctl status bluetooth      # Unit bluetooth.service could not be found.
ip link show wlan0              # Device "wlan0" does not exist.
ip a
sudo shutdown -h now
```

### Time source (jump host / NTP)

```bash
sudo tee /etc/systemd/timesyncd.conf > /dev/null <<'EOF'
[Time]
NTP=192.168.55.100
EOF
sudo systemctl restart systemd-timesyncd
timedatectl show-timesync --all
timedatectl timesync-status
```

### Benchmarks used in Experiment 1 (Pi)

```bash
cd $HOME
sudo apt update

# sbc-bench
curl -o sbc-bench.sh https://raw.githubusercontent.com/ThomasKaiser/sbc-bench/master/sbc-bench.sh
chmod +x sbc-bench.sh
sudo ./sbc-bench.sh -m           # Ctrl+C once confirmed live

# SciMark2 via the Phoronix Test Suite
sudo apt install -y unzip build-essential php-cli php-xml php-zip php-curl git
git clone https://github.com/phoronix-test-suite/phoronix-test-suite.git
cd phoronix-test-suite && sudo ./install-sh
phoronix-test-suite system-info
phoronix-test-suite benchmark pts/scimark2     # choose the quick option

# Geekbench 7 (ARM preview)
cd $HOME
curl -o geekbench7-arm.tar.gz https://cdn.geekbench.com/Geekbench-7.0.0-LinuxARMPreview.tar.gz
tar -xzf geekbench7-arm.tar.gz
cd Geekbench-7.0.0-LinuxARMPreview
file geekbench7
./geekbench7 --sysinfo
./geekbench7          # use a 5 A power supply: multi-core runs can brown out a weaker one

# transfer tools
which git rsync; git --version; rsync --version
```

---

## 3. DUT-PC (Intel N97)

- Ubuntu 24.04.5 LTS image; turn off all install options except OpenSSH.
- Hostname corrected to upper case:

```bash
sudo hostnamectl set-hostname DUT-PC
sudo cloud-init clean --logs
sudo reboot
```

- Update, then build OpenSSL 3.5.8 exactly as in section 2 (the build is much faster than on the Pi: a few minutes).
- Disable radios:

```bash
sudo apt install rfkill ethtool
sudo rfkill block wifi
sudo rfkill block bluetooth
sudo rfkill list
```

- Static wired address, `/etc/netplan/50-cloud-init.yaml`:

```yaml
network:
  version: 2
  ethernets:
    enp2s0:
      dhcp4: false
      addresses:
        - 192.168.55.17/24
      dhcp6: false
      accept-ra: false
      link-local: []
```

```bash
sudo netplan apply
ip a show enp2s0

# IPv6 off everywhere
sudo tee /etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
sudo sysctl --system

# force the changes
sudo netplan apply
sudo cloud-init clean --logs
sudo netplan apply
sudo cloud-init clean --logs
sudo reboot

# check
ip a
systemctl status bluetooth
ip link show wlp1s0
rfkill list
```

- OpenSSL persistence (on x86-64 the libraries are in `lib64`) and Geekbench 7 for x86:

```bash
sudo tee /etc/profile.d/openssl-local.sh > /dev/null <<'EOF'
export PATH=/usr/local/ssl/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/ssl/lib64:$LD_LIBRARY_PATH
EOF
source /etc/profile.d/openssl-local.sh
openssl version -a

cd $HOME
curl -O https://cdn.geekbench.com/Geekbench-7.0.0-Linux.tar.gz
tar -xzf Geekbench-7.0.0-Linux.tar.gz
cd Geekbench-7.0.0-Linux
file geekbench7
./geekbench7 --sysinfo
```

---

## 4. DUT-RISCV (Orange Pi RV2)

- Image: Canonical's Ubuntu developer image for the Orange Pi RV2
  (<https://canonical.com/blog/ubuntu-developer-images-now-available-for-orangepi-rv2-a-low-cost-risc-v-sbc>), downloaded from the Orange Pi website.
- The board gets very hot (the USB controller approaches 100 C), so active cooling is used throughout.

OpenSSL 3.5.8, same commands as section 2. Build time on this board: about 76 minutes for `make -j1`, and about 17 minutes for `make install`.

Make the new OpenSSL the default (the PATH was not persistent by default and the system OpenSSL was still used):

```bash
sudo bash -c 'cat > /etc/profile.d/openssl-local.sh <<EOF
export PATH="/usr/local/ssl/bin:\$PATH"
export LD_LIBRARY_PATH="/usr/local/ssl/lib:\$LD_LIBRARY_PATH"
EOF'
sudo reboot
openssl version -a
```

Reference results on this board: RSA-2048 about 87 signs/s; ML-KEM-768 about 2,258 keygens/s (CPU capabilities reported: `RV64GC_ZBA_ZBB_ZBC_ZBS_ZKT_V`, vector length 256).

Hostname and experiment account:

```bash
sudo hostnamectl set-hostname DUT-RISCV

sudo adduser <user>
sudo usermod -aG tty,disk,dialout,sudo,audio,video,plugdev,games,systemd-journal,input,netdev,docker <user>
groups <user>
sudo reboot
sudo whoami; whoami             # root, then <user>
```

Network: static wired address on `end0`, the DHCP interface (`end1`) is turned off. `/etc/netplan/orangepi-default.yaml` (set to mode 600):

```yaml
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    end0:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      addresses:
        - 192.168.55.15/24
    end1:
      dhcp4: true
```

```bash
sudo chmod 600 /etc/netplan/orangepi-default.yaml
sudo netplan apply

# radios off
sudo nmcli radio wifi off
ip a show wlan0                       # state DOWN
sudo systemctl status bluetooth       # Unit bluetooth.service could not be found.
sudo rfkill block bluetooth
rfkill list
echo "blacklist bcmdhd" | sudo tee /etc/modprobe.d/blacklist-bcmdhd.conf

# IPv6 off
sudo bash -c 'cat > /etc/sysctl.d/99-disable-ipv6.conf <<EOF
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF'
sudo sysctl --system
ip a

# turn off the DHCP / internet interface
nmcli connection show
sudo nmcli connection modify netplan-end1 connection.autoconnect no
sudo nmcli connection down netplan-end1
sudo ip link set end1 down

# no background updates during measurement
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service
```

Copy the system from the microSD card to the on-board storage (about 10 minutes), then remove the microSD:

```bash
which nand-sata-install
sudo nand-sata-install          # choose ext4
lsblk; hostnamectl; ip a; openssl version -a
```

Time source (chrony) and Geekbench 7 (RISC-V preview):

```bash
sudo nano /etc/chrony/chrony.conf
#   add, just above the pool lines:
#   server 192.168.55.100 iburst prefer
sudo systemctl restart chrony
chronyc sources -v
chronyc tracking

cd $HOME
curl -O https://cdn.geekbench.com/Geekbench-7.0.0-LinuxRISCVPreview.tar.gz
tar -xzf Geekbench-7.0.0-LinuxRISCVPreview.tar.gz
cd Geekbench-7.0.0-LinuxRISCVPreview
file geekbench7
./geekbench7 --sysinfo
./geekbench7
```

sbc-bench, SciMark2 and the transfer tools were installed on the RISC-V board as on the Pi (section 2).

---

## 5. Mac mini (M4)

The Mac mini runs macOS and is not built from these notes. See the dissertation (Chapters 3-4 and Appendix 4) for how it was configured and which controls (clock, fan, background services) were and were not available.
