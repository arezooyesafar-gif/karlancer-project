<?php
/**
 * Give the theme's mobile image slider its final layout from the first paint.
 *
 * The theme's slider widget (swip_article_slider, ".prk-mobile-image-slider")
 * is server-rendered as a plain row of full-width slides. Its script then
 * starts Swiper with "side peek" (1.2 or more slides per view, first slide
 * centred), which makes each slide narrower, so the square image shrinks
 * (412 px -> 232 px tall on a 412 px phone) and everything below the slider
 * jumps up by the difference: the 0.166 layout shift PageSpeed reports on the
 * mobile home page (".elementor-element-13473ac" moving 180 px).
 *
 * For each slider this adds a small scoped <style> before its markup that
 * lays out the not-yet-started slider exactly as Swiper will: the same slide
 * width ((width - (perView - 1) * gap) / perView), the same gap, the first
 * slide centred when Swiper centres it, and the image ratio of the first slide
 * (which the theme's script applies to all slides). The numbers come from the
 * settings the widget prints in data-prk-slider-settings, read the same way
 * the theme's script reads them. The rules only apply until Swiper adds
 * "swiper-initialized", so the running slider is untouched. Only the mobile
 * breakpoint (below 768 px) is pinned; desktop already has no shift. Sliders
 * in the theme's deferred (Ajax) mode, other effects and vertical sliders are
 * left alone. The styles are added to the finished page HTML (an output
 * buffer), not to the widget's own output, so Elementor's element cache cannot
 * keep a copy with or without them.
 *
 * Obeys 'slider_pin' (off / trial / on).
 */

defined( 'ABSPATH' ) || exit;

/**
 * The scoped CSS for one slider, or '' when it should be left alone.
 *
 * @param string $widget_id Elementor element id.
 * @param string $html      The widget's rendered HTML.
 */
function zioma_slider_pin_css( $widget_id, $html ) {
	if ( ! preg_match( '/^[a-z0-9]+$/i', $widget_id ) || false === strpos( $html, 'data-prk-mobile-image-slider' ) ) {
		return '';
	}
	if ( false !== strpos( $html, 'is-prk-mobile-image-slider-deferred' ) ) {
		return '';
	}
	if ( ! preg_match( '/data-prk-slider-settings="([^"]*)"/', $html, $m ) ) {
		return '';
	}
	$cfg = json_decode( html_entity_decode( $m[1], ENT_QUOTES, 'UTF-8' ), true );
	if ( ! is_array( $cfg ) ) {
		return '';
	}

	$truthy = function ( $value ) {
		return in_array( $value, array( true, 1, '1', 'true', 'yes', 'on' ), true );
	};
	$effect = isset( $cfg['effect'] ) && '' !== $cfg['effect'] ? (string) $cfg['effect'] : 'slide';
	if ( 'slide' !== $effect || ( isset( $cfg['direction'] ) && 'vertical' === $cfg['direction'] ) || $truthy( $cfg['autoHeight'] ?? false ) ) {
		return '';
	}

	// Mobile values as the theme's buildSwiperOptions() resolves them:
	// mobile, else tablet, else the main value.
	$pick = function ( $main, $tablet, $mobile, $fallback ) {
		$base = null !== $main ? $main : $fallback;
		$tab  = null !== $tablet ? $tablet : $base;
		return null !== $mobile ? $mobile : $tab;
	};
	$desktop_view = (float) ( $cfg['slidesPerView'] ?? 1 ) ?: 1;
	$tablet_view  = (float) ( $cfg['slidesPerViewTablet'] ?? 0 ) ?: $desktop_view;
	$per_view     = (float) ( $cfg['slidesPerViewMobile'] ?? 0 ) ?: $tablet_view;
	$side_peek    = $truthy( $cfg['sidePeek'] ?? false );
	if ( $side_peek ) {
		$per_view = max( 1.2, $per_view );
	}
	$gap      = (float) $pick( $cfg['spaceBetween'] ?? null, $cfg['spaceBetweenTablet'] ?? null, $cfg['spaceBetweenMobile'] ?? null, 0 );
	$centered = $side_peek || $truthy( $cfg['centeredSlides'] ?? false );
	$per_view = max( 1, $per_view );
	$gap      = max( 0, $gap );
	if ( 1.0 === $per_view && ! $centered ) {
		return '';
	}

	$num = function ( $value ) {
		return rtrim( rtrim( number_format( $value, 4, '.', '' ), '0' ), '.' ) ?: '0';
	};
	$spare = ( $per_view - 1 ) * $gap;
	$root  = '.elementor-element.elementor-element-' . $widget_id . ' .prk-mobile-image-slider.prk-mobile-image-slider:not(.is-prk-mobile-image-slider-deferred)';
	$track = $root . ' .prk-mobile-image-slider__swiper:not(.swiper-initialized)>.swiper-wrapper';
	$rules = array();

	if ( preg_match( '/--prk-mobile-image-slider-ratio:\s*([0-9.]+\s*\/\s*[0-9.]+)/', $html, $r ) ) {
		$rules[] = $root . '{--prk-mobile-image-slider-frame-ratio:' . preg_replace( '/\s+/', ' ', $r[1] ) . '}';
	}
	$rules[] = $track . '>.swiper-slide{flex-shrink:0;width:calc(' . $num( 100 / $per_view ) . '% - ' . $num( $spare / $per_view ) . 'px);margin-inline-end:' . $num( $gap ) . 'px}';
	if ( $centered ) {
		// Centre the first slide: (width - slide) / 2, towards the slider's start side.
		$shift   = 'calc(' . $num( ( 1 - 1 / $per_view ) * 50 ) . '% + ' . $num( $spare / ( 2 * $per_view ) ) . 'px)';
		$rules[] = $track . '{transform:translate3d(' . ( is_rtl() ? 'calc(-1 * ' . $shift . ')' : $shift ) . ',0,0)}';
	}

	return '<style id="zioma-slider-pin-' . $widget_id . '">@media (max-width:767.98px){' . implode( '', $rules ) . '}</style>';
}

/**
 * Adds the scoped style before every slider in a page.
 *
 * @param string $html Page HTML, or the part of it printed so far.
 */
function zioma_slider_pin_page( $html ) {
	if ( ! preg_match_all( '/<div\b[^>]*\bdata-prk-mobile-image-slider="1"[^>]*>/', $html, $tags, PREG_OFFSET_CAPTURE ) ) {
		return $html;
	}
	$starts = array_column( $tags[0], 1 );
	// From the last slider to the first, so earlier offsets stay valid.
	for ( $i = count( $starts ) - 1; $i >= 0; $i-- ) {
		$start = $starts[ $i ];
		$end   = isset( $starts[ $i + 1 ] ) ? min( $starts[ $i + 1 ], $start + 30000 ) : $start + 30000;
		// The widget's Elementor id is the nearest data-id before the slider.
		if ( ! preg_match_all( '/\bdata-id="([a-z0-9]+)"/i', substr( $html, max( 0, $start - 4000 ), min( $start, 4000 ) ), $ids ) ) {
			continue;
		}
		$css = zioma_slider_pin_css( end( $ids[1] ), substr( $html, $start, $end - $start ) );
		if ( '' !== $css ) {
			$html = substr( $html, 0, $start ) . $css . substr( $html, $start );
		}
	}

	return $html;
}

add_action(
	'template_redirect',
	function () {
		if ( ! zioma_perf_active() || ! zioma_mode_active( 'slider_pin' ) || is_feed() || is_customize_preview() || isset( $_GET['elementor-preview'] ) ) {
			return;
		}
		ob_start(
			function ( $html ) {
				if ( ! is_string( $html ) || false === strpos( $html, 'data-prk-mobile-image-slider' ) ) {
					return $html;
				}
				return zioma_slider_pin_page( $html );
			}
		);
	},
	PHP_INT_MAX
);
