<?php
/**
 * Inline "critical" CSS printed at the very top of <head>, before the theme's
 * stylesheets and long before the footer scripts.
 *
 * It exists only to pin a few elements to the layout state the theme itself
 * settles them into once its scripts run, so they do not sit in the page flow
 * at full height first and then vanish, shoving everything below them up or
 * down (cumulative layout shift). It must never change what the visitor sees:
 * every rule matches the element's settled, script-applied state, so the page
 * looks the same from the first paint instead of only after the JS catches up.
 *
 * Confirmed rules live in performance-config.php under 'critical_css_rules'
 * (a context => CSS map) and obey the 'critical_css' mode switch (off / trial
 * / on). Rules still being verified live under 'critical_css_trial_rules' and
 * are printed only on URLs with ?zioma_trial=1, whatever the main switch is, so
 * a new rule can be checked on the live site without touching the ones already
 * running for every visitor. Both honour the admin ?zioma_perf=off escape hatch.
 */

defined( 'ABSPATH' ) || exit;

add_action(
	'wp_head',
	function () {
		// The main query has run by wp_head, so the context checks are reliable.
		if ( ! did_action( 'wp' ) || ! zioma_perf_active() ) {
			return;
		}

		$config   = zioma_perf_config();
		$contexts = zioma_perf_contexts();
		$maps     = array();

		// Confirmed rules: gated by the off / trial / on switch.
		if ( zioma_mode_active( 'critical_css' ) && ! empty( $config['critical_css_rules'] ) ) {
			$maps[] = $config['critical_css_rules'];
		}

		// Staging rules: only on ?zioma_trial=1, and never when the whole feature is off.
		$mode = isset( $config['critical_css'] ) ? $config['critical_css'] : 'off';
		if ( 'off' !== $mode && isset( $_GET['zioma_trial'] ) && ! empty( $config['critical_css_trial_rules'] ) ) {
			$maps[] = $config['critical_css_trial_rules'];
		}

		$rules = array();
		foreach ( $maps as $map ) {
			foreach ( $contexts as $context ) {
				if ( ! empty( $map[ $context ] ) ) {
					$rules[] = trim( (string) $map[ $context ] );
				}
			}
		}

		$css = trim( implode( "\n", array_filter( $rules ) ) );
		if ( '' === $css ) {
			return;
		}

		// The CSS is author-controlled (performance-config.php), not user input.
		echo "\n<style id=\"zioma-critical\">" . $css . "</style>\n"; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped
	},
	1
);
