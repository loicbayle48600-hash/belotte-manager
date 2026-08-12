package fr.cuisine.haccp;

import android.os.Bundle;
import android.view.WindowManager;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Tablette de cuisine : écran maintenu allumé pour que les chronomètres
        // de refroidissement et leurs alarmes restent actifs (les timers de la
        // WebView sont gelés quand l'écran s'éteint). Aucune permission requise.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }
}
