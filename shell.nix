{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  name = "blogbot-dev";

  packages = with pkgs; [
    (python3.withPackages (ps: [
      ps.python-telegram-bot
    ]))
  ];

  shellHook = ''
    export BLOGBOT_SITE_ROOT="/home/d7tun6/files/mounts/TS480SSD/services/site/d7tun6"
    echo "blogbot dev shell ready"
  '';
}