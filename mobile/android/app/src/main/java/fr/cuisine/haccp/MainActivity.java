package fr.cuisine.haccp;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    // L'écran suit la mise en veille réglée dans Android (Paramètres →
    // Affichage). Pendant un refroidissement en cours, l'application garde
    // l'écran éveillé via l'API Wake Lock du web (voir refroidTick dans
    // app.js) pour que le chronomètre et l'alarme restent actifs.
}
