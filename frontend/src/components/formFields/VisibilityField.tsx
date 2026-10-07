import React from 'react';
import FormField from './FormField';
import { fieldClass, FIELD_FOCUS } from './formClasses';
import { useI18n } from '../../contexts/I18nContext';

export type Visibility = 'public' | 'private' | 'group-restricted';

interface VisibilityFieldProps {
  value: Visibility;
  onChange: (visibility: Visibility) => void;
  /** Comma-separated allowed groups (shown only for group-restricted). */
  allowedGroups: string;
  onAllowedGroupsChange: (value: string) => void;
  accent?: keyof typeof FIELD_FOCUS;
}

/**
 * Visibility select plus the conditional "Allowed Groups" input that appears
 * only for group-restricted visibility — the cascade duplicated in the agent
 * form and RegisterPage. (Skills use a simpler 3-option select without groups;
 * custom entities use a chip widget — those keep their own controls.)
 */
const VisibilityField: React.FC<VisibilityFieldProps> = ({
  value,
  onChange,
  allowedGroups,
  onAllowedGroupsChange,
  accent = 'purple',
}) => {
  const { t } = useI18n();
  return (
    <>
      <FormField label={t('Visibility')}>
        <select
          value={value}
          onChange={(e) => onChange(e.target.value as Visibility)}
          className={fieldClass(accent)}
        >
          <option value="private">{t('Private')}</option>
          <option value="public">{t('Public')}</option>
          <option value="group-restricted">{t('Group Restricted')}</option>
        </select>
      </FormField>

      {value === 'group-restricted' && (
        <FormField
          label={t('Allowed Groups')}
          hint={t('Comma-separated list of groups that can access this resource')}
        >
          <input
            type="text"
            value={allowedGroups}
            onChange={(e) => onAllowedGroupsChange(e.target.value)}
            className={fieldClass(accent)}
            placeholder={t('e.g. finance-team, engineering')}
          />
          {allowedGroups.trim() === '' && (
            <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
              {t('At least one group is required for group-restricted visibility')}
            </p>
          )}
        </FormField>
      )}
    </>
  );
};

export default VisibilityField;
