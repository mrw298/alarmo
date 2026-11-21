import { LitElement, html, CSSResultGroup, css } from 'lit';
import { property, customElement } from 'lit/decorators.js';
import { UnsubscribeFunc } from 'home-assistant-js-websocket';

import { commonStyle } from '../../styles';
import { localize } from '../../../localize/localize';
import { Dictionary, AlarmoArea, HomeAssistant, EConfirmationRule } from '../../types';
import { fetchAreas, saveArea } from '../../data/websockets';
import { handleError } from '../../helpers';
import { SubscribeMixin } from '../../subscribe-mixin';

import '../../components/alarmo-duration-picker';
import '../../components/alarmo-select';
import '../../components/alarmo-settings-row';

@customElement('confirmation-card')
export class ConfirmationCard extends SubscribeMixin(LitElement) {
  @property()
  hass!: HomeAssistant;

  @property({ type: Boolean })
  narrow!: boolean;

  @property()
  areas!: Dictionary<AlarmoArea>;

  @property()
  selectedArea!: string;

  public hassSubscribe(): Promise<UnsubscribeFunc>[] {
    this._fetchData();
    return [
      this.hass!.connection.subscribeMessage(() => this._fetchData(), {
        type: 'alarmo_config_updated',
      }),
    ];
  }

  private async _fetchData(): Promise<void> {
    if (!this.hass) return;
    this.areas = await fetchAreas(this.hass);
    if (!this.selectedArea || !(this.selectedArea in this.areas)) this.selectedArea = Object.keys(this.areas)[0];
  }

  render() {
    if (!this.areas || !this.selectedArea) return html``;
    const area = this.areas[this.selectedArea];
    const rule = area.confirmation_rule || EConfirmationRule.DifferentSensor;
    return html`
      <ha-card>
        <div class="card-header">
          <div class="name">${localize('panels.general.cards.confirmation.title', this.hass.language)}</div>
          ${Object.keys(this.areas).length > 1
            ? html`
                <alarmo-select
                  .hass=${this.hass}
                  .items=${Object.values(this.areas).map(e => Object({ value: e.area_id, name: e.name }))}
                  value=${this.selectedArea}
                  label=${this.hass.localize('ui.components.area-picker.area')}
                  @value-changed=${(ev: Event) => (this.selectedArea = (ev.target as HTMLInputElement).value)}
                ></alarmo-select>
              `
            : ''}
        </div>
        <div class="card-content">
          ${localize('panels.general.cards.confirmation.description', this.hass.language)}
        </div>

        <alarmo-settings-row .narrow=${this.narrow}>
          <span slot="heading">
            ${localize('panels.general.cards.confirmation.fields.confirmation_enabled.heading', this.hass.language)}
          </span>
          <span slot="description">
            ${localize('panels.general.cards.confirmation.fields.confirmation_enabled.description', this.hass.language)}
          </span>
          <ha-switch
            ?checked=${area.confirmation_enabled}
            @change=${(ev: Event) =>
              this.saveData(ev, { confirmation_enabled: (ev.target as HTMLInputElement).checked })}
          ></ha-switch>
        </alarmo-settings-row>

        ${area.confirmation_enabled
          ? html`
              <alarmo-settings-row .narrow=${this.narrow} nested>
                <span slot="heading">
                  ${localize('panels.general.cards.confirmation.fields.confirmation_time.heading', this.hass.language)}
                </span>
                <span slot="description">
                  ${localize(
                    'panels.general.cards.confirmation.fields.confirmation_time.description',
                    this.hass.language
                  )}
                </span>
                <alarmo-duration-picker
                  .hass=${this.hass}
                  max="7200"
                  step="60"
                  value=${area.confirmation_time || 1800}
                  @value-changed=${(ev: CustomEvent) => this.saveData(ev, { confirmation_time: ev.detail.value })}
                ></alarmo-duration-picker>
              </alarmo-settings-row>

              <alarmo-settings-row .narrow=${this.narrow} .large=${true} nested>
                <span slot="heading">
                  ${localize('panels.general.cards.confirmation.fields.confirmation_rule.heading', this.hass.language)}
                </span>
                <span slot="description">
                  ${localize(
                    `panels.general.cards.confirmation.fields.confirmation_rule.choose.${rule}.description`,
                    this.hass.language
                  )}
                </span>
                <alarmo-select
                  .hass=${this.hass}
                  .items=${Object.values(EConfirmationRule).map(e =>
                    Object({
                      value: e,
                      name: localize(
                        `panels.general.cards.confirmation.fields.confirmation_rule.choose.${e}.name`,
                        this.hass.language
                      ),
                    })
                  )}
                  value=${rule}
                  @value-changed=${(ev: Event) =>
                    this.saveData(ev, {
                      confirmation_rule: (ev.target as HTMLInputElement).value as EConfirmationRule,
                    })}
                ></alarmo-select>
              </alarmo-settings-row>

              <alarmo-settings-row .narrow=${this.narrow} nested>
                <span slot="heading">
                  ${localize(
                    'panels.general.cards.confirmation.fields.confirmation_require_reset.heading',
                    this.hass.language
                  )}
                </span>
                <span slot="description">
                  ${localize(
                    'panels.general.cards.confirmation.fields.confirmation_require_reset.description',
                    this.hass.language
                  )}
                </span>
                <ha-switch
                  ?checked=${area.confirmation_require_reset}
                  @change=${(ev: Event) =>
                    this.saveData(ev, { confirmation_require_reset: (ev.target as HTMLInputElement).checked })}
                ></ha-switch>
              </alarmo-settings-row>
            `
          : ''}
      </ha-card>
    `;
  }

  private saveData(ev: Event, update: Partial<AlarmoArea>) {
    const area = this.areas[this.selectedArea];
    if (Object.entries(update).every(([k, v]) => area[k as keyof AlarmoArea] === v)) return;
    this.areas = { ...this.areas, [this.selectedArea]: { ...area, ...update } };
    saveArea(this.hass, { area_id: this.selectedArea, ...update })
      .catch(e => handleError(e, ev.target as HTMLElement))
      .then();
  }

  static get styles(): CSSResultGroup {
    return css`
      ${commonStyle}
      alarmo-select {
        display: flex;
        min-width: 180px;
      }
    `;
  }
}
