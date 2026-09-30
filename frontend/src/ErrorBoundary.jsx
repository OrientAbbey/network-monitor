import { Component } from "react";

/**
 * Empêche qu'une exception de rendu ne blanchisse toute la page : affiche un
 * message clair avec un moyen de réessayer. (React exige un composant classe
 * pour les limites d'erreur.)
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("Erreur de rendu du dashboard", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" style={{ maxWidth: 560, margin: "60px auto", padding: 24, color: "#DCEAE3", fontFamily: "sans-serif" }}>
        <h1 style={{ fontSize: 20 }}>Une erreur est survenue dans l'affichage</h1>
        <p style={{ color: "#8FA89F" }}>{String(this.state.error?.message ?? this.state.error)}</p>
        <button type="button" onClick={() => this.setState({ error: null })} style={{ padding: "6px 14px", cursor: "pointer" }}>
          Réessayer
        </button>
      </div>
    );
  }
}
