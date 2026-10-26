import { serviceIcon } from "@/lib/format";
import type { Service } from "@/lib/types";

interface ServiceTileProps {
  service: Service;
}

/** Workplace service card — mirrors the legacy func-card markup. */
export default function ServiceTile({ service }: ServiceTileProps) {
  return (
    <a
      href={service.url}
      className="func-card"
      data-pal={service.name}
      target="_blank"
      rel="noopener noreferrer"
    >
      <span className="func-icon" aria-hidden="true">
        {serviceIcon(service.id)}
      </span>
      <h3>{service.name}</h3>
      <p className="product">{service.description}</p>
      <span className="open">Öffnen</span>
    </a>
  );
}
