# Copyright 2025 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit desktop xdg

DESCRIPTION="Deezer music streaming service - Linux port (Electron app)"
HOMEPAGE="https://github.com/aunetx/deezer-linux"
SRC_URI="
	amd64? (
		https://github.com/aunetx/deezer-linux/releases/download/v${PV}/deezer-desktop-${PV}-x64.tar.xz ->
		${P}-amd64.tar.xz )
	arm64? (
		https://github.com/aunetx/deezer-linux/releases/download/v${PV}/deezer-desktop-${PV}-arm64.tar.xz ->
		${P}-arm64.tar.xz )"

LICENSE="all-rights-reserved"
SLOT="0"
KEYWORDS="-* ~amd64 ~arm64"
RESTRICT="bindist mirror strip"

RDEPEND="
	dev-libs/atk
	dev-libs/expat
	dev-libs/glib:2
	dev-libs/nspr
	dev-libs/nss
	media-libs/alsa-lib
	media-libs/mesa
	net-print/cups
	x11-libs/cairo
	x11-libs/gtk+:3
	x11-libs/libX11
	x11-libs/libXcomposite
	x11-libs/libXdamage
	x11-libs/libXext
	x11-libs/libXfixes
	x11-libs/libXrandr
	x11-libs/libxcb
	x11-libs/pango
"

S="${WORKDIR}"
QA_PREBUILT="opt/deezer/*"

src_unpack() {
	default
	# electron-builder productName may be "deezer" or "Deezer" depending on the release
	local appdir
	appdir=$(ls -d "${WORKDIR}"/deezer* 2>/dev/null | head -1)
	[[ -n "${appdir}" ]] || appdir=$(ls -d "${WORKDIR}"/Deezer* 2>/dev/null | head -1)
	[[ -n "${appdir}" ]] || die "No app dir found in ${WORKDIR}. Contents: $(ls ${WORKDIR})"
	mv "${appdir}" "${WORKDIR}/deezer-app" || die
}

src_install() {
	dodir /opt/deezer
	cp -a "${WORKDIR}/deezer-app/." "${ED}/opt/deezer/" || die

	make_wrapper deezer "/opt/deezer/deezer-desktop"

	local desktop_src="${WORKDIR}/deezer-app/resources/dev.aunetx.deezer.desktop"
	if [[ -f "${desktop_src}" ]]; then
		sed \
			-e 's|^Exec=.*|Exec=/opt/deezer/deezer-desktop %U|' \
			-e 's|^TryExec=.*|TryExec=/opt/deezer/deezer-desktop|' \
			"${desktop_src}" > "${T}/dev.aunetx.deezer.desktop" || die
		domenu "${T}/dev.aunetx.deezer.desktop"
	fi

	local icon_src="${WORKDIR}/deezer-app/resources/dev.aunetx.deezer.svg"
	[[ -f "${icon_src}" ]] && doicon -s scalable "${icon_src}"
}

pkg_postinst() { xdg_pkg_postinst; }
pkg_postrm()   { xdg_pkg_postrm; }
