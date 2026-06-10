import { useEffect, useState } from "react";
import { supabase } from "./supabaseClient";
import type { User } from "@supabase/supabase-js";

const ADMIN_EMAILS = ["malvoamadeus@gmail.com", "mandywang8866@gmail.com"];

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      return;
    }

    supabase.auth.getSession().then(({ data }) => {
      setUser(data.session?.user ?? null);
      setLoading(false);
    });

    const { data: listener } = supabase.auth.onAuthStateChange((_event, session) => {
      setUser(session?.user ?? null);
    });

    return () => {
      listener.subscription.unsubscribe();
    };
  }, []);

  const signInWithGoogle = () => {
    supabase?.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: window.location.origin
      }
    });
  };

  const signOut = () => {
    supabase?.auth.signOut();
  };

  const isAdmin = ADMIN_EMAILS.includes(user?.email ?? "");

  return { user, isAdmin, loading, signInWithGoogle, signOut };
}
