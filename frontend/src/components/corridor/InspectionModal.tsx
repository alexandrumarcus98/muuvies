import { Modal } from '../Modal';
import type { CorridorMediaItem } from './mediaCatalog';

interface InspectionModalProps {
  item: CorridorMediaItem | null;
  onClose: () => void;
  onLaunchSearch: (title: string) => void;
}

export function InspectionModal({ item, onClose, onLaunchSearch }: InspectionModalProps) {
  return (
    <Modal
      open={item !== null}
      onClose={onClose}
      label={item ? `Inspect ${item.title}` : 'Inspect print'}
      panelClassName="cr-inspect-modal"
    >
      {item && (
        <div className="cr-inspect-grid">
          <div className="cr-inspect-media">
            <img src={item.imageSrc} alt={`${item.title} (${item.year}) still`} />
          </div>

          <div className="cr-inspect-body">
            <div className="cr-inspect-top">
              <span className="cr-label cr-accent-text">
                Print {String(item.index).padStart(2, '0')} · {item.year}
              </span>
              <button
                type="button"
                className="cr-btn cr-btn-ghost"
                onClick={onClose}
                aria-label="Close inspection"
              >
                Close
              </button>
            </div>

            <h3 className="cr-inspect-title">{item.title}</h3>
            <p className="cr-inspect-director">{item.director}</p>

            <div className="cr-inspect-specs">
              <div>
                <span className="cr-label cr-muted">Aspect</span>
                <span className="cr-spec-value">{item.aspect}</span>
              </div>
              <div>
                <span className="cr-label cr-muted">Master</span>
                <span className="cr-spec-value">{item.specs}</span>
              </div>
            </div>

            <p className="cr-inspect-synopsis">{item.synopsis}</p>

            <div className="cr-inspect-footer">
              <button
                type="button"
                className="cr-btn cr-btn-solid"
                onClick={() => onLaunchSearch(item.title)}
              >
                Search this title
              </button>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}

